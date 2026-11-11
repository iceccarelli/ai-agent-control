"""An AST scan for numeric literals in FEE contexts of production Python. It is a TRIPWIRE for 'a fee number was typed somewhere new', not a proof that no fee is wrong:
the fee RATE authority is ``carry_costs`` (published VIP-0 table) and, for an account, the signed ``/v5/account/fee-rate`` read; this scan only forces every other literal to be
declared (file, symbol, value, reason) in an allow-list so a new or changed one is a reviewed decision.

Contexts: (1) assignment to a fee-named target (literal, dict/tuple of literals, or an expression: a conditional / product / call -> every non-unit-conversion constant inside);
(2) keyword argument with a fee-named keyword; (3) positional arguments of a callee whose NAME is fee-named (set_exit_fee_bps(5.5, 10.0)); (4) a dict display with a fee-named string key,
including lab(5.5, 'assumed') style labelled values; (5) a function default for a fee-named parameter; (6) parse_float(env, 'TAKER_FEE_BPS', default...) environment defaults.
All-zero literals (0 / 0.0 'no fee' initialisers) are structural and ignored. Tests, __pycache__ and data directories are not scanned; a file that does not parse FAILS the scan.
"""
from __future__ import annotations

import ast
import os
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional, Tuple

SKIP_DIRS = {"tests", "__pycache__", "state", "runs", "data", "artifacts", "node_modules"}
FEE_NAME = re.compile(r"(^|_)(fee|fees|taker|maker|rebate|commission|round_trip)(_|$)|FeeRate|feeRate|TAKER|MAKER", re.I)
NOT_FEE = re.compile(r"(age|ttl|_ms$|_s$|count|limit_|max_|min_|digest|schema|path|source|kind|status|flag|cap$|cap_|symbol|ccy|currency|timeout|seconds)", re.I)
CONVERSIONS = {Decimal(x) for x in ("0", "1", "100", "1000", "10000")}

Hit = Tuple[str, int, str, str, Tuple[str, ...]]            # (file, line, context, name, values)


class FeeScanError(Exception):
    pass


def _num(n: ast.AST) -> Optional[Decimal]:
    if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool):
        return Decimal(repr(n.value)) if isinstance(n.value, float) else Decimal(n.value)
    if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.USub, ast.UAdd)):
        v = _num(n.operand)
        return None if v is None else (-v if isinstance(n.op, ast.USub) else v)
    if isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", None)) in ("D", "Decimal") and n.args and isinstance(n.args[0], ast.Constant):
        try:
            return Decimal(str(n.args[0].value))
        except InvalidOperation:
            return None
    return None


def _subtree(n: ast.AST) -> List[Decimal]:
    """constants inside an EXPRESSION: only floats and Decimal('..') count. An int inside an expression is a count (2 legs, 4 rounding places), never a fee rate; a fee typed as a bare int
    is still caught when it is the whole value (x_fee_bps = 10)."""
    out = []
    for c in ast.walk(n):
        if isinstance(c, ast.Constant) and isinstance(c.value, int) and not isinstance(c.value, bool):
            continue
        v = _num(c) if isinstance(c, (ast.Constant, ast.UnaryOp, ast.Call)) else None
        if v is not None and v > 0 and v not in CONVERSIONS:
            out.append(v)
    return out


def _values(n: ast.AST) -> List[Decimal]:
    """numeric literals of a value expression: a literal, a collection of literals, a labelled value lab(5.5, ...), or (for an expression) every non-conversion constant inside it"""
    v = _num(n)
    if v is not None:
        return [v]
    if isinstance(n, ast.Dict):
        return [x for val in n.values for x in _values(val)]
    if isinstance(n, (ast.Tuple, ast.List)):
        return [x for e in n.elts for x in _values(e)]
    if isinstance(n, ast.Call) and n.args and getattr(n.func, "id", "") in ("lab", "label", "Quantity", "Q") and _num(n.args[0]) is not None:
        return [_num(n.args[0])]                                      # type: ignore[list-item]
    if isinstance(n, (ast.IfExp, ast.BinOp)) or (isinstance(n, ast.Call) and not isinstance(n.func, ast.Attribute)):
        return _subtree(n)
    return []


def _fee_name(s: Optional[str]) -> bool:
    return bool(s) and bool(FEE_NAME.search(s)) and not NOT_FEE.search(s)          # type: ignore[arg-type]


def scan_source(src: str, rel: str) -> List[Hit]:
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        raise FeeScanError(f"{rel}: cannot parse ({exc}): a file the scan cannot read is a failure, not a skip")
    hits: List[Hit] = []

    def add(line: int, ctx: str, name: str, vals: Iterable[Decimal]) -> None:
        vs = [v for v in vals if v != 0]
        if vs:
            hits.append((rel, line, ctx, name, tuple(str(v.normalize()) if v == v.to_integral_value() and False else str(v) for v in vs)))

    for n in ast.walk(tree):
        if isinstance(n, (ast.Assign, ast.AnnAssign)) and getattr(n, "value", None) is not None:
            for t in (n.targets if isinstance(n, ast.Assign) else [n.target]):
                nm = t.id if isinstance(t, ast.Name) else (t.attr if isinstance(t, ast.Attribute) else None)
                if _fee_name(nm):
                    add(n.lineno, "assign", nm, _values(n.value))                                        # type: ignore[arg-type]
        if isinstance(n, ast.Call):
            for kw in n.keywords:
                if _fee_name(kw.arg):
                    add(n.lineno, "kwarg", kw.arg, _values(kw.value))                                    # type: ignore[arg-type]
            callee = getattr(n.func, "id", getattr(n.func, "attr", ""))
            if _fee_name(callee):
                add(n.lineno, "call_args", callee, [x for a in n.args for x in _values(a)])
            if callee in ("parse_float", "parse_optional_float", "parse_fraction") and len(n.args) >= 2 and isinstance(n.args[1], ast.Constant) and isinstance(n.args[1].value, str) \
                    and FEE_NAME.search(n.args[1].value):
                add(n.lineno, "env_default", n.args[1].value, [x for a in n.args[2:] for x in _values(a)])
        if isinstance(n, ast.Dict):
            for k, v in zip(n.keys, n.values):
                if isinstance(k, ast.Constant) and isinstance(k.value, str) and _fee_name(k.value):
                    add(getattr(k, "lineno", n.lineno), "dict", k.value, _values(v))
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a = n.args
            pos = a.posonlyargs + a.args
            for arg, d in zip(pos[len(pos) - len(a.defaults):], a.defaults):
                if _fee_name(arg.arg):
                    add(n.lineno, "default", f"{n.name}({arg.arg})", _values(d))
            for arg, d in zip(a.kwonlyargs, a.kw_defaults):
                if d is not None and _fee_name(arg.arg):
                    add(n.lineno, "default", f"{n.name}({arg.arg})", _values(d))
    return hits


def scan(root: str) -> List[Hit]:
    out: List[Hit] = []
    for r, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f.endswith(".py"):
                p = os.path.join(r, f)
                with open(p, encoding="utf-8") as fh:
                    out += scan_source(fh.read(), os.path.relpath(p, root))
    return sorted(set(out))


def inventory(root: str) -> Dict[Tuple[str, str, str], Tuple[str, ...]]:
    """{(file, context, name): sorted distinct values} -- line numbers are deliberately NOT part of the key (an allow-list must survive unrelated edits)."""
    inv: Dict[Tuple[str, str, str], set] = {}
    for rel, _line, ctx, name, vals in scan(root):
        inv.setdefault((rel, ctx, name), set()).update(vals)
    return {k: tuple(sorted(v, key=lambda s: Decimal(s))) for k, v in inv.items()}
