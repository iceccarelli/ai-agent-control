"""Round 21: every command-line help string is valid under argparse's own %-formatting, on every supported interpreter.

REPRODUCED (Round 21, the owner's Codespace on Python 3.14): `python3 tools/ms_testnet_autopilot.py status` died in `_parser()` with `ValueError: badly formed help string` BEFORE any status
action, because the help text for --notional-usd contained a literal `40%`. Python 3.14's argparse validates a help string when the argument is ADDED (earlier versions only when --help is printed), so
the same defect was latent on 3.11-3.13 and fatal on 3.14. A second CLI (owner_ledger, `%/yr`) had the same defect. Two tests: a static scan of every constant `help=` in shipped code (stdlib only,
so it fails on any interpreter the suite runs on) and a dynamic check that the owner-facing CLIs build and print their help on the interpreter running the suite."""
from __future__ import annotations

import ast
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)

#: what argparse passes to `help % params` (HelpFormatter._expand_help): any other %-sequence in a help string is a defect.
PARAMS = {"prog": "prog", "default": None, "type": None, "choices": None, "dest": "dest", "metavar": None, "nargs": None, "required": False, "const": None, "help": "h", "option_strings": [],
          "container": None}
ADD_CALLS = ("add_argument", "add_parser", "add_argument_group", "add_mutually_exclusive_group")


def static_help_text(node):
    """(text, notes) for a help expression whose text is knowable without running code, else None. A plain string; an f-string (each `{expr}` stands in as `X`, and a format spec that itself emits a `%`, such as
    `:.0%`, is a defect because the formatted text then carries an unescaped percent); an implicit or explicit `+` concatenation of those. A help built from a variable or a call is not knowable here (the dynamic
    parser-factory test below covers it by building the parser)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value, []
    if isinstance(node, ast.JoinedStr):
        text, notes = "", []
        for v in node.values:
            if isinstance(v, ast.Constant):
                text += str(v.value)
                continue
            spec = getattr(v, "format_spec", None)
            if spec is not None and any(isinstance(x, ast.Constant) and "%" in str(x.value) for x in spec.values):
                notes.append("an f-string format spec emits a literal % (write {x * 100:.0f}%% instead of {x:.0%})")
            text += "X"
        return text, notes
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = static_help_text(node.left), static_help_text(node.right)
        if left is not None and right is not None:
            return left[0] + right[0], left[1] + right[1]
    return None


def bad_help_strings(src: str, path: str = "<src>"):
    """(path, line, error, text) for every statically knowable help string argparse would refuse. A help containing `%` must say `%%` (or `%(default)s` and the other named fields)."""
    out = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ADD_CALLS:
            for kw in node.keywords:
                if kw.arg != "help":
                    continue
                known = static_help_text(kw.value)
                if known is None:
                    continue
                text, notes = known
                errs = list(notes)
                try:
                    text % PARAMS
                except Exception as exc:  # noqa: BLE001 - the point is to catch every way argparse can reject it
                    errs.append(f"{type(exc).__name__}: {exc}")
                for e in errs:
                    out.append((path, node.lineno, e, text[:100]))
    return out


def _shipped_python():
    for root, dirs, files in os.walk(BOT):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in ("node_modules", "__pycache__", "site-packages") and not os.path.exists(os.path.join(root, d, "pyvenv.cfg")))
        for name in sorted(files):
            if name.endswith(".py"):
                yield os.path.join(root, name)


def test_the_detector_sees_a_literal_percent_in_every_spelling_argparse_refuses():
    planted = {
        "percent_of": 'p.add_argument("--n", help="the size (40% of the ceiling)")',
        "percent_slash": 'p.add_argument("--r", help="return, %/yr")',
        "percent_trailing": 'p.add_argument("--x", help="100%")',
        "subparser": 's.add_parser("go", help="uses 5% of it")',
        "group": 'p.add_argument_group("g", description="x", help="7% x")',
        # Round 21: found by the full Python 3.14 run, not by the first version of this detector (train_policy.py --holdout-from)
        "fstring_format_spec": 'p.add_argument("--h", help="ho " f"Default: the last {FRAC:.0%} of the span.")',
        "fstring_literal_percent": 'p.add_argument("--h", help=f"the last 20% of {name}")',
        "concatenated": 'p.add_argument("--h", help="a " + "20% of it")',
        "implicit_concat": 'p.add_argument("--h", help="the size "\n "(40% of it)")',
    }
    for kind, src in planted.items():
        assert bad_help_strings(src), f"the detector missed {kind}"
    fine = ('p.add_argument("--n", help="the size (40%% of the ceiling, default %(default)s, prog %(prog)s)")\np.add_argument("--m", help="no percent here")\n'
            'p.add_argument("--f", help=f"the last {FRAC * 100:.0f}%% of the span ({N:d} bars)")')
    assert bad_help_strings(fine) == []


def test_no_shipped_help_string_contains_an_unescaped_percent():
    files = list(_shipped_python())
    assert len(files) > 200, "the walker found no shipped code: the scan would pass vacuously"
    offenders = []
    checked = 0
    for p in files:
        src = open(p, encoding="utf-8").read()
        if "add_argument" not in src and "add_parser" not in src:
            continue
        checked += 1
        offenders += bad_help_strings(src, os.path.relpath(p, BOT))
    assert checked > 100
    assert not offenders, "argparse (Python 3.14 at add_argument time) refuses these help strings; write `%%` for a literal percent:\n" + "\n".join(f"  {p}:{ln} {err}  [{txt}]" for p, ln, err, txt in offenders)


#: the CLIs an owner types at a terminal. `--help` exits 0 before any action, any credential read, any network call or any file write; a parser that cannot be built is a CLI that cannot even say so.
OWNER_CLIS = ("tools/ms_testnet_autopilot.py", "tools/ms_testnet_session.py", "tools/owner_ledger.py", "tools/owner_import.py", "tools/e1_status.py", "tools/ms_pipeline.py",
              "tools/candidate_competition.py", "tools/image_manifest.py", "tools/md_seal.py", "tools/market_recorder.py", "tools/runner_monitor.py", "tools/carry_replay.py",
              "tools/history_holdout.py", "tools/close_testnet_short.py", "tools/control_centre.py", "tools/train_policy.py")


@pytest.mark.parametrize("rel", OWNER_CLIS)
def test_the_owner_facing_clis_build_and_print_their_help_on_this_interpreter(rel, tmp_path):
    if not os.path.isfile(os.path.join(BOT, rel)):
        pytest.skip(f"{rel} does not exist in this tree")
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1", "LEDGER_DISABLED": "1",
           "HISTORY_HOLDOUT_LEDGER": str(tmp_path / "holdout.jsonl"), "CANDIDATE_LEDGER_DIR": str(tmp_path / "cand"), "CAPITAL_ANCHOR_DIR": str(tmp_path / "anchor")}
    r = subprocess.run([sys.executable, os.path.join(BOT, rel), "--help"], capture_output=True, text=True, timeout=120, cwd=BOT, env=env)
    assert r.returncode == 0, (rel, r.returncode, r.stderr[-600:])
    assert "badly formed help string" not in r.stderr and "Traceback" not in r.stderr
    assert "usage" in r.stdout.lower()
    assert not os.path.exists(tmp_path / "holdout.jsonl") and not os.path.exists(tmp_path / "cand") and not os.path.exists(tmp_path / "anchor"), "--help must not touch any owner state"


def test_the_autopilot_parser_formats_every_subcommands_help_without_a_credential_or_a_network(tmp_path, monkeypatch):
    sys.path[:0] = [BOT, os.path.join(BOT, "tools")]
    import ms_testnet_autopilot as A
    import socket

    def boom(*_a, **_k):
        raise AssertionError("network touched")
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    parser = A._parser()
    text = parser.format_help()
    assert "usage" in text.lower()
    subs = [a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction"]
    assert subs, "the autopilot has sub-commands"
    for name, sub in subs[0].choices.items():
        assert "usage" in sub.format_help().lower(), name
    assert "40%" in subs[0].choices["evaluate"].format_help(), "the evaluate help shows the literal percent the source spells `%%`"


# ----------------------------------------------------------------------------------------- the dynamic net: build every parser a shipped module can build
PARSER_FACTORY_NAMES = {"build_parser", "_build_parser", "make_parser", "_make_parser", "_parser", "parser", "get_parser", "_get_parser", "arg_parser", "_arg_parser"}


def _parser_factories():
    """(module name, function name) for every shipped top-level function named like a parser factory and callable with no arguments."""
    out = []
    for p in _shipped_python():
        rel = os.path.relpath(p, BOT)
        if rel.startswith("tests" + os.sep) or os.sep in rel and not rel.startswith("tools" + os.sep):
            continue
        src = open(p, encoding="utf-8").read()
        if "ArgumentParser" not in src:
            continue
        for node in ast.parse(src).body:
            if isinstance(node, ast.FunctionDef) and node.name in PARSER_FACTORY_NAMES:
                a = node.args
                required = len(a.args) - len(a.defaults) + sum(1 for d in a.kw_defaults if d is None)
                if required == 0:
                    out.append((os.path.splitext(os.path.basename(p))[0], node.name, rel))
    return out


def test_every_shipped_parser_factory_builds_and_formats_its_help_on_this_interpreter():
    """Python 3.14 validates every help string when the argument is added, so BUILDING the parser is the check; a help built from a variable or a call (invisible to the static scan) fails here. The full Python 3.14
    suite found train_policy.py's f-string help this way; this makes the finding a standing test."""
    sys.path[:0] = [BOT, os.path.join(BOT, "tools")]
    import importlib
    factories = _parser_factories()
    assert len(factories) >= 4, factories              # pinned to what exists (parsers built inside main() are covered by the static scan and the owner-CLI --help run): fewer means the walker broke
    built, failures = 0, []
    for mod, fn, rel in factories:
        try:
            m = importlib.import_module(mod)
        except ImportError:                       # an optional dependency absent on this interpreter: not this test's subject
            continue
        try:
            parser = getattr(m, fn)()
            parser.format_help()
            built += 1
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{rel}:{fn}: {type(exc).__name__}: {exc}")
    assert built >= 3, (built, factories)
    assert not failures, "\n".join(failures)


# ----------------------------------------------------------------------------------------- the sweep: every argparse script prints its help (run on the NEWEST interpreters by the python-compat workflow)
SWEEP_RC_OK = {"tools/runner_supervisor.py": {0, 3}}         # rc 3 is its usage convention when no `--` command follows


def _argparse_scripts():
    out = []
    for p in _shipped_python():
        rel = os.path.relpath(p, BOT)
        if rel.startswith("tests" + os.sep):
            continue
        src = open(p, encoding="utf-8").read()
        if "ArgumentParser" in src and "__main__" in src:
            out.append(rel)
    return out


@pytest.mark.skipif(not os.environ.get("HELP_SWEEP"), reason="set HELP_SWEEP=1 (the python-compat workflow does): ~150 subprocesses")
def test_every_argparse_script_prints_its_help_on_this_interpreter(tmp_path):
    """The review's net: a help built from a variable or a call is invisible to the static scan, and a parser built inside main() is invisible to the factory test, but `--help` builds every parser."""
    scripts = _argparse_scripts()
    assert len(scripts) > 80, len(scripts)
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1", "LEDGER_DISABLED": "1",
           "HISTORY_HOLDOUT_LEDGER": str(tmp_path / "holdout.jsonl"), "CANDIDATE_LEDGER_DIR": str(tmp_path / "cand"), "CAPITAL_ANCHOR_DIR": str(tmp_path / "anchor")}
    bad = []
    for rel in scripts:
        r = subprocess.run([sys.executable, os.path.join(BOT, rel), "--help"], capture_output=True, text=True, timeout=120, cwd=BOT, env=env, stdin=subprocess.DEVNULL)
        if r.returncode not in SWEEP_RC_OK.get(rel, {0}) or "Traceback" in r.stderr or "badly formed help string" in r.stderr:
            bad.append((rel, r.returncode, r.stderr[-200:]))
    assert not bad, bad
    assert not os.path.exists(tmp_path / "holdout.jsonl") and not os.path.exists(tmp_path / "cand") and not os.path.exists(tmp_path / "anchor"), "--help must not touch any owner state"
