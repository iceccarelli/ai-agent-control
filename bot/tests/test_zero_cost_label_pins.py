"""A cost that is ZERO and carries a provenance that is not an OBSERVATION is the loophole the capital audit found in the executor's gate: ``economic_model`` refuses only an 'assumed' input, so a zero
labelled 'modelled' or 'derived' passes as if it were a cost (``opportunity_cost_apr = Quantity(0, 'modelled')``: an unset owner cost of capital became free capital).

WHAT THIS IS. A source-level TRIPWIRE with a reviewed ALLOW-LIST of every place in production code that constructs a ``Quantity`` whose value is a statically-known zero and whose provenance is not 'measured'
or 'venue_stated' (or cannot be resolved statically). Each entry names the file, the enclosing function, the normalised call, and the REASON it is acceptable. A NEW site fails the test until someone
reviews it and writes the reason down; an entry whose site disappeared fails the test until it is deleted (the list tracks reality); an extra identical site inside an allowed scope fails the count check.
It changes no behaviour. An entry that is NOT acceptable but not yet fixed is marked KNOWN LOOPHOLE so it stays visible (the executor gate's modelled-zero cost of capital was one; it is fixed and no
loophole entry remains: ``test_no_known_loophole_is_left...``).

WHAT IT CATCHES (each has a mutation test below that plants it in a temporary tree). Positional and keyword arguments, ``from economic_model import Quantity as Q``, module aliases (``EM.Quantity``),
``Q = Quantity`` aliases, named zero constants at module, class or function level (resolved through a scope-aware, single-binding analysis: a name counts as a constant only if EVERY binding of it is the same
constant; accumulators, loop/with/comprehension/walrus/tuple-unpack targets, parameters, ``global``/``nonlocal`` rebinding and shadowing all make it NOT a constant, so ``total = Decimal(0); total += x`` is
not reported), exponent / signed / padded string zeros, ``Decimal()``, ``int()``, unary signs, folded arithmetic (``D(1) - D(1)``, ``5 * 0``, ``0 * x``), ``max/min/abs/round`` of zeros, a ternary whose arms are
both zero, ``.quantize()`` and other zero-preserving Decimal methods, a provenance spelled through a constant, and a provenance that is not statically resolvable (reported as DYNAMIC: it has to be justified
too). Floats, bools and padded strings are refused by ``economic_model.to_decimal`` at runtime; the scanner reports them anyway.

WHAT IT CANNOT PROVE (stated, not hidden). It does not see a zero produced at runtime (``Quantity(x - x, ...)`` with a non-constant ``x``, a value read from a file, ``dataclasses.replace``), an accumulator
that happens to stay zero, a zero imported from another module as a bare name (``from cfg import ZERO``) or reached as an attribute (``cfg.ZERO``, ``self.ZERO``), a zero inside ``eval``/``exec``/``getattr``
indirection, or a cost that is simply never charged. A ``*args``/``**kwargs`` call is reported as UNRESOLVED_ARGS (it must be justified) rather than guessed. Whether a missing economic input can become a
free cost is a RUNTIME property; it is enforced by behavioural tests of the model and of the executor's gate, not by this file.
"""
import ast
import os
import sys
from decimal import Decimal, InvalidOperation
from typing import Dict, List, NamedTuple, Optional, Sequence, Set, Tuple, Union

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: provenance labels under which a zero is a genuine OBSERVATION (it was measured / stated by the venue); any other label, or an unresolvable one, makes a zero look like a cost
REAL_ZERO_LABELS = ("measured", "venue_stated")
SKIP_DIRS = ("tests", "__pycache__", "state", "runs", "data", "artifacts")
NUMERIC_CTORS = ("Decimal", "D", "to_decimal", "int", "float")
ZERO_PRESERVING_METHODS = ("quantize", "normalize", "copy_abs", "copy_negate", "to_integral_value", "to_integral", "sqrt")


class Site(NamedTuple):
    path: str            # relative to the scanned root
    scope: str           # enclosing qualified name ("<module>" at top level)
    code: str            # the normalised call
    kind: str            # LABELLED_ZERO:<label> | DYNAMIC_PROVENANCE_ZERO | UNRESOLVED_ARGS
    line: int


# ------------------------------------------------------------------------------------------------ constant folding
Const = Union[Decimal, str]


def _fold(node: ast.AST, env: Dict[str, Const]) -> Optional[Decimal]:
    """The exact numeric value of a constant expression, or None when it is not one this scanner can fold."""
    if isinstance(node, ast.Constant):
        v = node.value
        if isinstance(v, bool):
            return None
        if isinstance(v, (int, float)):
            try:
                return Decimal(str(v))
            except InvalidOperation:
                return None
        if isinstance(v, str):
            try:
                return Decimal(v.strip())
            except InvalidOperation:
                return None
        return None
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        x = _fold(node.operand, env)
        return None if x is None else (-x if isinstance(node.op, ast.USub) else x)
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
        a, b = _fold(node.left, env), _fold(node.right, env)
        if isinstance(node.op, ast.Mult) and ((a is not None and a == 0) or (b is not None and b == 0)):
            return Decimal(0)                                                   # 0 * anything finite is 0 (also when the other factor is not a constant)
        if isinstance(node.op, ast.Div) and a is not None and a == 0:
            return Decimal(0)
        if a is None or b is None:
            return None
        if isinstance(node.op, ast.Add):
            return a + b
        if isinstance(node.op, ast.Sub):
            return a - b
        if isinstance(node.op, ast.Mult):
            return a * b
        return None if b == 0 else a / b
    if isinstance(node, ast.IfExp):                                             # both arms the same constant: the condition does not matter
        a, b = _fold(node.body, env), _fold(node.orelse, env)
        return a if a is not None and a == b else None
    if isinstance(node, ast.Name):
        v = env.get(node.id)
        if isinstance(v, Decimal):
            return v
        if isinstance(v, str):
            try:
                return Decimal(v.strip())
            except InvalidOperation:
                return None
        return None
    if isinstance(node, ast.Call):
        name = node.func.id if isinstance(node.func, ast.Name) else (node.func.attr if isinstance(node.func, ast.Attribute) else None)
        if name in NUMERIC_CTORS:
            if not node.args and not node.keywords:
                return Decimal(0)                                               # Decimal() == int() == float() == 0
            if node.args:
                return _fold(node.args[0], env)
        if name in ("abs", "max", "min", "round") and node.args and not node.keywords:
            vals = [_fold(a, env) for a in node.args]
            if any(v is None for v in vals):
                return None
            return {"abs": lambda: abs(vals[0]), "max": lambda: max(vals), "min": lambda: min(vals), "round": lambda: vals[0]}[name]()
        if isinstance(node.func, ast.Attribute) and node.func.attr in ZERO_PRESERVING_METHODS:
            return _fold(node.func.value, env)
    return None


def _is_zero(node: ast.AST, env: Dict[str, Const]) -> bool:
    v = _fold(node, env)
    return v is not None and v.is_finite() and v == 0


def _label(node: Optional[ast.AST], env: Dict[str, Const]) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        v = env.get(node.id)
        return v if isinstance(v, str) else None
    return None


def _value(node: ast.AST, env: Dict[str, Const]) -> Optional[Const]:
    """What a plain assignment binds, when it is a constant: a string stays a string (it may be a provenance label), anything else folds to a Decimal."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and isinstance(env.get(node.id), str):
        return env[node.id]                                                     # type: ignore[return-value]
    return _fold(node, env)


class _Binder(ast.NodeVisitor):
    """Every binding of every simple name in ONE scope (it does not descend into nested functions, lambdas or classes): name -> the assigned value nodes, with None for any binding that is not a plain
    ``name = value`` (augmented assignment, loop / with / comprehension / walrus / tuple-unpack target, import, ``except ... as``, ``del``, a parameter, a nested def or class of that name, a match capture)."""

    def __init__(self) -> None:
        self.bindings: Dict[str, List[Optional[ast.AST]]] = {}

    def add(self, name: str, value: Optional[ast.AST]) -> None:
        self.bindings.setdefault(name, []).append(value)

    def visit_Assign(self, node):                                              # noqa: N802
        for t in node.targets:
            if isinstance(t, ast.Name):
                self.add(t.id, node.value)
            else:
                self.visit(t)                                                  # tuple/list/starred targets: visit_Name records them as non-constant bindings
        self.visit(node.value)

    def visit_AnnAssign(self, node):                                           # noqa: N802
        if isinstance(node.target, ast.Name):
            if node.value is not None:
                self.add(node.target.id, node.value)
        else:
            self.visit(node.target)
        if node.value is not None:
            self.visit(node.value)

    def visit_Name(self, node):                                                # noqa: N802
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.add(node.id, None)

    def visit_FunctionDef(self, node):                                         # noqa: N802
        self.add(node.name, None)

    visit_AsyncFunctionDef = visit_FunctionDef                                 # noqa: N815

    def visit_ClassDef(self, node):                                            # noqa: N802
        self.add(node.name, None)

    def visit_Lambda(self, node):                                              # noqa: N802
        return None

    def visit_Import(self, node):                                              # noqa: N802
        for a in node.names:
            self.add((a.asname or a.name).split(".")[0], None)

    visit_ImportFrom = visit_Import                                            # noqa: N815

    def visit_ExceptHandler(self, node):                                       # noqa: N802
        if node.name:
            self.add(node.name, None)
        self.generic_visit(node)

    def visit_MatchAs(self, node):                                             # noqa: N802
        if node.name:
            self.add(node.name, None)
        self.generic_visit(node)

    visit_MatchStar = visit_MatchAs                                            # noqa: N815

    def visit_MatchMapping(self, node):                                        # noqa: N802
        if node.rest:
            self.add(node.rest, None)
        self.generic_visit(node)


def _frame(body: List[ast.stmt], params: Sequence[str], env_outer: Dict[str, Const], rebound: Set[str]):
    """(constants, bound): a name is a constant of this scope only when EVERY binding of it is a plain assignment of the same constant (an accumulator, a loop variable, a parameter or a rebinding is not),
    and it is never declared ``global``/``nonlocal`` anywhere. ``bound`` is every name this scope binds at all (it shadows an outer constant)."""
    b = _Binder()
    for p in params:
        b.add(p, None)
    for stmt in body:
        b.visit(stmt)
    bound = set(b.bindings)
    env = dict(env_outer)
    for n in bound:
        env.pop(n, None)                                                       # a local binding shadows an outer constant until proven constant here
    consts: Dict[str, Const] = {}
    for _ in range(len(bound) + 1):                                            # fixed point: Z2 = ZERO resolves once ZERO has
        changed = False
        for n in sorted(bound):
            if n in consts or n in rebound or any(v is None for v in b.bindings[n]):
                continue
            vals = [_value(v, env) for v in b.bindings[n]]                     # type: ignore[arg-type]
            if all(v is not None for v in vals) and all(type(v) is type(vals[0]) and v == vals[0] for v in vals):
                consts[n] = vals[0]                                            # type: ignore[assignment]
                env[n] = vals[0]                                               # type: ignore[assignment]
                changed = True
        if not changed:
            break
    return consts, bound


def _rebound_names(tree: ast.AST) -> Set[str]:
    out: Set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Global, ast.Nonlocal)):
            out.update(n.names)
    return out


# ------------------------------------------------------------------------------------------------ the scan
class _Scanner(ast.NodeVisitor):
    def __init__(self, path: str, tree: ast.Module) -> None:
        self.path = path
        self.sites: List[Site] = []
        self.q_names = {"Quantity"}
        self.rebound = _rebound_names(tree)
        self.stack: List[str] = []
        self.frames: List[Tuple[Dict[str, Const], Set[str], str]] = []           # (constants, bound names, kind)
        for n in ast.walk(tree):                                               # `from economic_model import Quantity as Q`
            if isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[-1] == "economic_model":
                for a in n.names:
                    if a.name == "Quantity":
                        self.q_names.add(a.asname or a.name)
        for n in tree.body:                                                    # `Q = Quantity` / `Q = EM.Quantity` at module level
            if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
                if (isinstance(n.value, ast.Name) and n.value.id in self.q_names) or (isinstance(n.value, ast.Attribute) and n.value.attr == "Quantity"):
                    self.q_names.add(n.targets[0].id)
        consts, bound = _frame(tree.body, (), {}, self.rebound)
        self.frames.append((consts, bound, "module"))

    def _env(self) -> Dict[str, Const]:
        env: Dict[str, Const] = {}
        last = len(self.frames) - 1
        for i, (consts, bound, kind) in enumerate(self.frames):
            if kind == "class" and i != last:
                continue                                                       # a class body's names are not visible to the functions inside it
            for n in bound:
                env.pop(n, None)
            env.update(consts)
        return env

    def _push(self, body, params, kind: str, name: str) -> None:
        consts, bound = _frame(body, params, self._env(), self.rebound)
        self.frames.append((consts, bound, kind))
        self.stack.append(name)

    def _pop(self) -> None:
        self.frames.pop()
        self.stack.pop()

    def visit_FunctionDef(self, node):                                         # noqa: N802
        a = node.args
        params = [x.arg for x in (a.posonlyargs + a.args + a.kwonlyargs)] + [x.arg for x in (a.vararg, a.kwarg) if x]
        for d in a.defaults + [d for d in a.kw_defaults if d is not None] + node.decorator_list:
            self.visit(d)                                                      # defaults and decorators run in the enclosing scope
        self._push(node.body, params, "function", node.name)
        for stmt in node.body:
            self.visit(stmt)
        self._pop()

    visit_AsyncFunctionDef = visit_FunctionDef                                 # noqa: N815

    def visit_Lambda(self, node):                                              # noqa: N802
        a = node.args
        params = [x.arg for x in (a.posonlyargs + a.args + a.kwonlyargs)] + [x.arg for x in (a.vararg, a.kwarg) if x]
        self.frames.append(({}, set(params), "function"))
        self.stack.append("<lambda>")
        self.visit(node.body)
        self._pop()

    def visit_ClassDef(self, node):                                            # noqa: N802
        for d in node.bases + node.decorator_list:
            self.visit(d)
        self._push(node.body, (), "class", node.name)
        for stmt in node.body:
            self.visit(stmt)
        self._pop()

    def visit_Call(self, node):                                                # noqa: N802
        f = node.func
        is_q = (isinstance(f, ast.Name) and f.id in self.q_names) or (isinstance(f, ast.Attribute) and f.attr == "Quantity")
        if is_q:
            self._check(node)
        self.generic_visit(node)

    def _check(self, call: ast.Call) -> None:
        env = self._env()
        scope = ".".join(self.stack) or "<module>"
        code = ast.unparse(call)
        starred = any(isinstance(a, ast.Starred) for a in call.args) or any(k.arg is None for k in call.keywords)
        kw = {k.arg: k.value for k in call.keywords if k.arg}
        value = call.args[0] if call.args and not isinstance(call.args[0], ast.Starred) else kw.get("value")
        prov = call.args[1] if len(call.args) > 1 and not isinstance(call.args[1], ast.Starred) else kw.get("provenance")
        if starred and (value is None or prov is None):
            self.sites.append(Site(self.path, scope, code, "UNRESOLVED_ARGS", call.lineno))
            return
        if value is None or not _is_zero(value, env):
            return
        label = _label(prov, env)
        if label in REAL_ZERO_LABELS:
            return
        self.sites.append(Site(self.path, scope, code, f"LABELLED_ZERO:{label}" if label is not None else "DYNAMIC_PROVENANCE_ZERO", call.lineno))


def scan(root: str) -> List[Site]:
    out: List[Site] = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for f in sorted(files):
            if not f.endswith(".py"):
                continue
            path = os.path.join(dirpath, f)
            try:
                tree = ast.parse(open(path, encoding="utf-8").read())
            except (SyntaxError, UnicodeDecodeError):
                continue
            s = _Scanner(os.path.relpath(path, root), tree)
            s.visit(tree)
            out.extend(s.sites)
    return out


# ------------------------------------------------------------------------------------------------ the reviewed allow-list
#: (file, enclosing scope, normalised call) -> why this zero is acceptable (or the tracked reason it is not YET fixed). A reason is REQUIRED and is read by a human at review time.
ALLOWED = {
    ("economic_model.py", "qsum", "Quantity(Decimal(0), 'derived', unit)"):
        "accumulator seed of a sum: 0 + q is q, and an unknown element makes the whole sum unknown (Quantity arithmetic); it is never reported on its own",
    ("economic_model.py", "_leg_costs", "Quantity(Decimal(0), p.provenance, 'bps')"):
        "a MAKER leg that does not cross has zero spread/slippage only as strongly as its stated fill probability: the provenance is the probability's (never upgraded to 'derived'); an unknown "
        "probability returns unknown(); no production caller builds a maker leg (the autopilot gate is taker-only), so this is exercised by the economic-model tests only",
    ("economic_model.py", "evaluate_chain", "Quantity(Decimal(0), 'derived', 'bps')"):
        "STRUCTURAL zero: with no maker leg in the scenario there is no adverse selection and no maker-fill shortfall to charge (two sites: adverse_selection and maker_fill); with a maker leg "
        "the scenario's own (possibly unknown) inputs are used instead",
    ("economic_model.py", "capacity_usd", "Quantity(Decimal(0), prov, 'usd')"):
        "a RESULT, not a cost: 'the largest notional that still clears the floor is 0' when even the smallest order fails; its provenance is the weakest of the chain's known components",
    ("tools/ms_edge_gate.py", "evaluate_edge", "EM.Quantity(D(0), 'derived', 'fraction')"):
        "STRUCTURAL zero: the spot leg is bought with USDT, not borrowed, so there is no borrow cost to charge",
    ("tools/ms_edge_gate.py", "evaluate_edge", "EM.Quantity(D(0), 'assumed', 'fraction')"):
        "REJECTION-ONLY PROBE: with the owner's cost of capital UNKNOWN the edge is re-priced with that one input floored at zero and labelled ASSUMED (the model refuses to PASS an assumed input). Its net is "
        "compared with the hurdle to REJECT a hopeless edge and is never reported or used to pass: a cost of capital can only lower the net, so a miss at zero is a miss at any rate. Pinned by "
        "test_ms_edge_gate.py::test_with_an_unknown_cost_of_capital_no_funding_level_can_make_a_profit_candidate",
}


#: how many times the same (file, scope, call) may legitimately appear; anything else is an unreviewed extra
EXPECTED_COUNT = {("economic_model.py", "evaluate_chain", "Quantity(Decimal(0), 'derived', 'bps')"): 2}


def _key(s: Site):
    return (s.path, s.scope, s.code)


def review(root: str):
    """(sites by key, keys not allow-listed, allow-listed keys that no longer exist, keys whose multiplicity is not the reviewed one)."""
    found: Dict[tuple, List[Site]] = {}
    for s in scan(root):
        found.setdefault(_key(s), []).append(s)
    new = sorted(k for k in found if k not in ALLOWED)
    stale = sorted(k for k in ALLOWED if k not in found)
    multi = sorted(k for k, v in found.items() if k in ALLOWED and len(v) != EXPECTED_COUNT.get(k, 1))
    return found, new, stale, multi


def test_zero_valued_quantities_without_an_observed_provenance_exist_only_where_they_are_reviewed():
    found, new, stale, multi = review(BOT)
    assert not new, ("a NEW zero-valued Quantity without an observed provenance appeared in production code: " + "; ".join(f"{k[0]}:{found[k][0].line} in {k[1]}: {k[2]} [{found[k][0].kind}]" for k in new)
                     + ". economic_model refuses only 'assumed', so a labelled zero passes as a cost. Use a measured / venue_stated value, keep the input UNKNOWN, or (for a STRUCTURAL zero) add it to ALLOWED "
                       "with the reason it is real.")
    assert not stale, f"an allow-listed zero no longer exists: delete its entry so the list tracks reality: {stale}"
    assert not multi, f"an allow-listed zero now appears a different number of times than reviewed (expected {EXPECTED_COUNT}): {multi}"
    assert all(isinstance(r, str) and len(r) >= 40 for r in ALLOWED.values()), "every allow-list entry needs a real reason"


def test_no_known_loophole_is_left_and_the_one_rejection_probe_is_labelled_as_such():
    """The allow-list is an exception list, so its composition is a metric. The executor gate's modelled-zero opportunity cost (an unset owner cost of capital priced as FREE) was removed: an unstated cost
    of capital is UNKNOWN now (test_ms_edge_gate.py). No entry may be a KNOWN LOOPHOLE; the single REJECTION-ONLY probe is the assumed zero that can reject but never pass."""
    assert [k for k, why in ALLOWED.items() if why.startswith("KNOWN LOOPHOLE")] == []
    assert [k for k, why in ALLOWED.items() if why.startswith("REJECTION-ONLY PROBE")] == [("tools/ms_edge_gate.py", "evaluate_edge", "EM.Quantity(D(0), 'assumed', 'fraction')")]
    assert not any(k[2].endswith("'modelled', 'fraction')") for k in ALLOWED), "a modelled zero cost is the loophole's exact shape"


# ------------------------------------------------------------------------------------------------ mutation tests: every bypass the scanner claims to catch must be caught
def _plant(tmp_path, body: str) -> List[Site]:
    d = tmp_path / "bot" / "tools"
    d.mkdir(parents=True, exist_ok=True)
    (d / "planted.py").write_text("from decimal import Decimal, Decimal as D\nimport economic_model as EM\nfrom economic_model import Quantity\n" + body)
    return scan(str(tmp_path / "bot"))


BYPASSES = {
    "positional literal": "x = Quantity(Decimal(0), 'modelled', 'bps')",
    "keyword value and provenance": "x = Quantity(value=Decimal(0), provenance='modelled', unit='bps')",
    "positional value, keyword provenance": "x = Quantity(Decimal(0), provenance='assumed')",
    "module alias attribute": "x = EM.Quantity(D('0'), 'derived', 'fraction')",
    "aliased import": "from economic_model import Quantity as Q\nx = Q(Decimal(0), 'modelled', 'bps')",
    "alias by assignment": "Q2 = Quantity\nx = Q2(Decimal(0), 'modelled', 'bps')",
    "named zero constant": "ZERO = Decimal(0)\nx = Quantity(ZERO, 'modelled', 'bps')",
    "named zero chain": "ZERO = Decimal(0)\nZ2 = ZERO\nx = Quantity(Z2, 'derived', 'bps')",
    "function-local zero constant": "def f():\n    z = Decimal('0')\n    return Quantity(z, 'modelled', 'bps')",
    "exponent zero string": "x = Quantity(Decimal('0E-8'), 'modelled', 'bps')",
    "padded zero string": "x = Quantity(' 0 ', 'modelled', 'bps')",
    "decimal zero with places": "x = Quantity(Decimal('0.000'), 'derived', 'bps')",
    "negative zero": "x = Quantity(-Decimal(0), 'modelled', 'bps')",
    "int zero": "x = Quantity(0, 'modelled', 'bps')",
    "float zero": "x = Quantity(0.0, 'modelled', 'bps')",
    "empty decimal": "x = Quantity(Decimal(), 'modelled', 'bps')",
    "folded subtraction": "x = Quantity(Decimal(1) - Decimal(1), 'modelled', 'bps')",
    "folded product": "x = Quantity(5 * 0, 'modelled', 'bps')",
    "zero times an unknown factor": "def f(a):\n    return Quantity(Decimal(0) * a, 'modelled', 'bps')",
    "ternary of two zeros": "def f(c):\n    return Quantity(Decimal(0) if c else Decimal('0.0'), 'modelled', 'bps')",
    "max of zeros": "x = Quantity(max(Decimal(0), Decimal(0)), 'modelled', 'bps')",
    "abs of a negative zero": "x = Quantity(abs(Decimal('-0')), 'modelled', 'bps')",
    "zero rebound to the same zero": "def f():\n    z = Decimal(0)\n    z = Decimal('0.0')\n    return Quantity(z, 'modelled', 'bps')",
    "zero bound in a class body": "class C:\n    Z = Decimal(0)\n    X = Quantity(Z, 'modelled', 'bps')",
    "zero in a lambda": "f = lambda: Quantity(Decimal(0), 'modelled', 'bps')",
    "zero in a nested function": "def f():\n    def g():\n        return Quantity(Decimal(0), 'modelled', 'bps')\n    return g",
    "zero in a default argument expression": "def f(q=Quantity(Decimal(0), 'modelled', 'bps')):\n    return q",
    "module zero seen from a function": "ZERO = Decimal(0)\ndef f():\n    return Quantity(ZERO, 'modelled', 'bps')",
    "outer function zero seen from a nested one": "def f():\n    z = Decimal(0)\n    def g():\n        return Quantity(z, 'modelled', 'bps')\n    return g",
    "quantize of zero": "x = Quantity(Decimal(0).quantize(Decimal('0.01')), 'modelled', 'bps')",
    "provenance through a named constant": "PROV = 'modelled'\nx = Quantity(Decimal(0), PROV, 'bps')",
    "provenance through a local constant": "def f():\n    p = 'derived'\n    return Quantity(Decimal(0), p, 'bps')",
    "dynamic provenance": "def f(p):\n    return Quantity(Decimal(0), p.provenance, 'bps')",
    "nested in a scenario call": "x = dict(opportunity_cost_apr=Quantity(Decimal(0), 'modelled', 'fraction'))",
    "star args": "def f(a):\n    return Quantity(*a)",
    "keyword dict": "def f(k):\n    return Quantity(**k)",
}


def test_the_scanner_catches_every_documented_bypass(tmp_path):
    missed = []
    for name, body in BYPASSES.items():
        sub = tmp_path / name.replace(" ", "_")
        if not _plant(sub, body):
            missed.append(name)
    assert not missed, f"the scanner missed: {missed}"


def test_the_scanner_does_not_flag_what_is_not_a_labelled_zero(tmp_path):
    clean = {
        "non-zero": "x = Quantity(Decimal('0.0001'), 'modelled', 'bps')",
        "measured zero": "x = Quantity(Decimal(0), 'measured', 'bps')",
        "venue-stated zero": "x = Quantity(Decimal(0), 'venue_stated', 'bps')",
        "unknown": "x = EM.unknown('bps')",
        "named non-zero": "ONE = Decimal(1)\nx = Quantity(ONE, 'modelled', 'bps')",
        "computed non-constant": "def f(a):\n    return Quantity(a, 'modelled', 'bps')",
        "a different class named Q": "class Q:\n    pass\nx = Q(Decimal(0), 'modelled', 'bps')",
        "measured through a constant": "P = 'measured'\nx = Quantity(Decimal(0), P, 'bps')",
        "an accumulator that starts at zero": "def f(items):\n    total = Decimal(0)\n    for i in items:\n        total += i\n    return Quantity(total, 'derived', 'usd')",
        "a zero reassigned to a non-zero before use": "def f():\n    z = Decimal(0)\n    z = Decimal(5)\n    return Quantity(z, 'modelled', 'bps')",
        "a zero rebound by a conditional": "def f(c):\n    z = Decimal(0)\n    if c:\n        z = Decimal(5)\n    return Quantity(z, 'modelled', 'bps')",
        "a parameter shadowing a module zero": "ZERO = Decimal(0)\ndef f(ZERO):\n    return Quantity(ZERO, 'modelled', 'bps')",
        "a loop variable shadowing a module zero": "ZERO = Decimal(0)\ndef f(xs):\n    for ZERO in xs:\n        return Quantity(ZERO, 'modelled', 'bps')",
        "a module zero rebound through global": "ZERO = Decimal(0)\ndef g():\n    global ZERO\n    ZERO = Decimal(5)\nx = Quantity(ZERO, 'modelled', 'bps')",
        "a tuple-unpacked name": "def f(t):\n    z, w = Decimal(0), t\n    return Quantity(z, 'modelled', 'bps')",
        "a class zero not visible in a method": "class C:\n    Z = Decimal(0)\n    def m(self, Z):\n        return Quantity(Z, 'modelled', 'bps')",
        "a lambda parameter shadowing a module zero": "ZERO = Decimal(0)\nf = lambda ZERO: Quantity(ZERO, 'modelled', 'bps')",
        "an unknown constructor is not Quantity": "x = Qty(Decimal(0), 'modelled', 'bps')",
    }
    flagged = [name for name, body in clean.items() if _plant(tmp_path / name.replace(" ", "_"), body)]
    assert not flagged, f"false positives: {flagged}"


def test_a_site_is_reported_with_its_scope_kind_and_normalised_code(tmp_path):
    sites = _plant(tmp_path, "def evaluate():\n    return Quantity(value=Decimal('0.0'), provenance='modelled', unit='fraction')")
    assert len(sites) == 1 and sites[0].scope == "evaluate" and sites[0].kind == "LABELLED_ZERO:modelled" and sites[0].path.endswith("planted.py")
    assert sites[0].code == "Quantity(value=Decimal('0.0'), provenance='modelled', unit='fraction')"


def _copy_real(tmp_path):
    import shutil
    root = tmp_path / "bot"
    (root / "tools").mkdir(parents=True)
    shutil.copy(os.path.join(BOT, "economic_model.py"), root / "economic_model.py")
    shutil.copy(os.path.join(BOT, "tools", "ms_edge_gate.py"), root / "tools" / "ms_edge_gate.py")
    return root


def test_the_real_review_passes_on_a_copy_of_the_two_production_files_it_pins(tmp_path):
    root = _copy_real(tmp_path)
    _found, new, stale, multi = review(str(root))
    assert not new and not multi and not stale, (new, multi, stale)


def test_a_new_labelled_zero_planted_in_a_real_production_file_fails_the_pin(tmp_path):
    root = _copy_real(tmp_path)
    gate = root / "tools" / "ms_edge_gate.py"
    gate.write_text(gate.read_text() + "\n\ndef _planted():\n    return EM.Quantity(value=D('0E-8'), provenance='modelled', unit='fraction')\n")
    _found, new, _stale, _multi = review(str(root))
    assert [k[1] for k in new] == ["_planted"], new


def test_a_second_identical_zero_inside_an_already_allowed_scope_fails_the_count_check(tmp_path):
    root = _copy_real(tmp_path)
    gate = root / "tools" / "ms_edge_gate.py"
    gate.write_text(gate.read_text() + "\n\ndef evaluate_edge():\n    return EM.Quantity(D(0), 'assumed', 'fraction')\n")
    _found, new, _stale, multi = review(str(root))
    assert not new and multi == [("tools/ms_edge_gate.py", "evaluate_edge", "EM.Quantity(D(0), 'assumed', 'fraction')")], (new, multi)


def test_removing_an_allowed_zero_from_a_real_file_makes_its_entry_stale(tmp_path):
    root = _copy_real(tmp_path)
    gate = root / "tools" / "ms_edge_gate.py"
    text = gate.read_text()
    assert "EM.Quantity(D(0), \"assumed\", \"fraction\")" in text or "EM.Quantity(D(0), 'assumed', 'fraction')" in text
    gate.write_text(text.replace("EM.Quantity(D(0), \"assumed\", \"fraction\")", "EM.unknown(\"fraction\")").replace("EM.Quantity(D(0), 'assumed', 'fraction')", "EM.unknown('fraction')"))
    _found, _new, stale, _multi = review(str(root))
    assert ("tools/ms_edge_gate.py", "evaluate_edge", "EM.Quantity(D(0), 'assumed', 'fraction')") in stale


def test_reintroducing_the_free_capital_loophole_in_the_real_gate_fails_the_pin(tmp_path):
    """The exact original shape: an opportunity_cost_apr keyword given a modelled ZERO. It must be reported as a NEW site (there is no allow-list entry for it any more)."""
    root = _copy_real(tmp_path)
    gate = root / "tools" / "ms_edge_gate.py"
    gate.write_text(gate.read_text() + "\n\ndef _regression():\n    return dict(opportunity_cost_apr=EM.Quantity(D(0), \"modelled\", \"fraction\"))\n")
    _found, new, _stale, _multi = review(str(root))
    assert [(k[1], k[2]) for k in new] == [("_regression", "EM.Quantity(D(0), 'modelled', 'fraction')")], new
