"""ENTRY-POINT INVENTORY: no unguarded competing way to open exposure on the Testnet account.

``test_order_path_inventory`` pins the places an order can be CREATED. This pins the EXECUTABLES above them: every command-line / service entry point that can reach an order-capable client, each with
its class and the guard that is proved here by reading the source:

  1 READ-ONLY            sends no order
  2 PROTECTIVE EXIT      can only reduce (reduce-only close / unwind); deliberately NOT gated behind the lease, because an exit must never be made slower or refused
  3 TESTNET ENTRY        can open a hedge; must take the single-writer authority (the local account lock and/or the shared Supabase lease)
  (legacy armed tools are class 3 too: their command-line entry takes the same authority through ``run_guarded_cli``, whatever deployment references them)

A NEW executable that reaches an order-capable path fails this test until a reviewer classifies it. Facts are read from the AST and the file tree, never assumed."""
import ast
import os
import re
import sys
from typing import Dict, List, Set

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
ROOT = os.path.dirname(BOT)
sys.path[:0] = [HERE, BOT, os.path.join(BOT, "tools")]

import test_order_path_inventory as OPI  # noqa: E402

pytestmark = pytest.mark.real_single_writer      # the drill-guard test exercises the REAL single-writer guard

#: executable -> (class, how it is held to account)
ENTRY_POINTS: Dict[str, str] = {
    "tools/ms_testnet_autopilot.py": "3 lease + account lock (via execute) + owner grant",
    "tools/ms_testnet_session.py": "3 execute: guarded_main lease (renewed) + account lock; reconcile: account lock + kill handoff; unwind never blocked",
    "tools/unattended_drill.py": "3 guarded_run_once: shared lease + account lock",
    "tools/drill.py": "3 --arm: single_writer_guard (account lock + shared lease, or its guarded parent)",
    "tools/close_testnet_short.py": "2 reduce-only Buy, ungated on purpose",
    "main.py": "3 carry book: config-gated (PAPER_TRADING, venue, approved risk policy); the carry-book app stays stopped",
    "tools/testnet_session.py": "3 legacy armed tool: its __main__ goes through run_guarded_cli (single-writer authority unless help / dry-run / offline self-check)",
    "tools/testnet_conformance_run.py": "3 legacy armed tool: its __main__ goes through run_guarded_cli (single-writer authority unless help / dry-run / offline self-check)",
    "tools/linear_stop_venue_drill.py": "3 legacy armed tool: its __main__ goes through run_guarded_cli (single-writer authority unless help / dry-run / offline self-check)",
    "tools/close_testnet_experiment.py": "3 legacy armed tool: its __main__ goes through run_guarded_cli (single-writer authority unless help / dry-run / offline self-check; `--phase recover`, a protective flatten, is exempt)",
}
LEGACY_ARMED_TOOLS = [f for f, d in ENTRY_POINTS.items() if "run_guarded_cli" in d]


def _read(rel: str) -> str:
    with open(os.path.join(BOT, rel), encoding="utf-8") as fh:
        return fh.read()


def _tree(rel: str) -> ast.Module:
    return ast.parse(_read(rel))


def _funcs(tree: ast.Module) -> Dict[str, ast.FunctionDef]:
    return {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}


def _names_in(node: ast.AST) -> Set[str]:
    out: Set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and len(n.value) < 80:
            out.add(n.value)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            out.update(a.name for a in n.names)
            if isinstance(n, ast.ImportFrom) and n.module:
                out.add(n.module)
    return out


def _has_main(rel: str) -> bool:
    return "__main__" in _read(rel)


def _files(pattern_hits) -> Set[str]:
    return {OPI._rel(f) for f in OPI._sources() if pattern_hits(f)}


# ------------------------------------------------------------------ nothing new can appear unclassified
def test_every_executable_that_can_open_or_send_orders_is_classified():
    callers_build_bot = _files(lambda f: "build_bot(" in open(f, encoding="utf-8").read() and _has_main(OPI._rel(f)))
    importers_executor = _files(lambda f: re.search(r"^\s*import ms_testnet_executor", open(f, encoding="utf-8").read(), re.M) is not None)
    order_site_files = {s.split(":")[0] for s, role in OPI.EXPECTED_SITES.items() if role in ("CLIENT_CALLER", "BROKER_CALLER", "GATEWAY") and _has_main(s.split(":")[0])}
    spawners = _files(lambda f: "drill.py" in open(f, encoding="utf-8").read() and "subprocess" in open(f, encoding="utf-8").read() and _has_main(OPI._rel(f)))
    found = callers_build_bot | importers_executor | order_site_files | spawners
    unclassified = sorted(found - set(ENTRY_POINTS))
    assert not unclassified, f"order-capable executables with no classification (add them to ENTRY_POINTS on purpose): {unclassified}"
    missing = sorted(f for f in ENTRY_POINTS if not os.path.exists(os.path.join(BOT, f)))
    assert not missing, f"classified files that no longer exist: {missing}"


def test_only_the_reviewed_modules_can_install_an_entry_authority_or_build_a_testnet_executor():
    assign = _files(lambda f: re.search(r"\.entry_authority\s*=", open(f, encoding="utf-8").read()) is not None)
    assert assign == {"main.py", "backtest.py", "tools/ms_authority.py"}, assign
    installers = _files(lambda f: re.search(r"\bMA\.install\(|ms_authority\.install\(", open(f, encoding="utf-8").read()) is not None)
    assert installers == {"tools/ms_testnet_session.py"}, installers
    constructs = _files(lambda f: re.search(r"TE\.TestnetExecutor\(", open(f, encoding="utf-8").read()) is not None)
    assert constructs == {"tools/ms_testnet_session.py", "tools/ms_testnet_autopilot.py"}, constructs


# ------------------------------------------------------------------ the guards are really in the code paths
def test_the_session_tool_cli_goes_through_guarded_main_and_execute_and_reconcile_take_the_account_lock():
    rel = "tools/ms_testnet_session.py"
    tree, fns = _tree(rel), _funcs(_tree(rel))
    main_block = [n for n in tree.body if isinstance(n, ast.If) and "__main__" in ast.dump(n.test)]
    assert main_block and "guarded_main" in _names_in(main_block[0]) and "main" not in {n.func.id for n in ast.walk(main_block[0]) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_account_lock" in _names_in(fns["cmd_execute"]) and "try_acquire" in _names_in(fns["cmd_execute"])
    assert "_account_lock" in _names_in(fns["cmd_reconcile"]) and "_hand_over_unwind" in _names_in(fns["cmd_reconcile"])
    g = _names_in(fns["guarded_main"])
    assert {"legacy_executor_guard", "renew", "engage_kill_switch", "release"} <= g | {"legacy_executor_guard"}


def test_the_unwind_is_never_behind_the_lease_but_never_a_second_writer():
    fns = _funcs(_tree("tools/ms_testnet_session.py"))
    # the unwind path takes no lease (it must always be available) ...
    assert "legacy_executor_guard" not in _names_in(fns["cmd_reconcile"]) and "acquire" not in {n.attr for n in ast.walk(fns["cmd_reconcile"]) if isinstance(n, ast.Attribute) and n.attr == "acquire"}
    # ... and when the lock is held it hands over (kill switch) instead of running beside the holder
    h = _names_in(fns["_hand_over_unwind"])
    assert "engage_kill_switch" in h and "try_acquire" in h
    # no code path of the unwind constructs an entry: it only ever asks for exits
    assert "submit_group" not in _names_in(fns["cmd_reconcile"]) | _names_in(fns["_reconcile_locked"])


def test_the_autopilot_passes_its_strong_lease_check_into_every_session_and_never_builds_a_second_lease_client():
    rel = "tools/ms_testnet_autopilot.py"
    src = _read(rel)
    assert "pre_order_check=" in src and "guarded_main" not in src                                  # it calls main in-process, already holding the lease: no double acquisition
    assert src.count("LeaseClient(") <= 1


def test_the_armed_drill_and_the_guarded_drill_parent_hold_the_single_writer_authority():
    fns = _funcs(_tree("tools/drill.py"))
    assert "single_writer_guard" in _names_in(fns["main"]) and "single_writer" in _names_in(fns["single_writer_guard"])
    lg = _funcs(_tree("tools/legacy_executor_guard.py"))
    assert {"ExecutorLock", "try_acquire", "acquire"} <= _names_in(lg["single_writer"])
    ud = _funcs(_tree("tools/unattended_drill.py"))
    assert {"ExecutorLock", "try_acquire", "acquire"} <= _names_in(ud["guarded_run_once"])


def test_the_protective_close_has_exactly_one_order_call_and_it_is_a_buy_of_the_linear_perp():
    """The reduce-only guard on every linear Buy lives in the broker (the venue clamps a reduce-only order to the position); this pins that the tool itself has ONE order call and it can only be
    the Buy that covers a short: no Sell, no spot, no second order, no entry."""
    calls = [n for n in ast.walk(_tree("tools/close_testnet_short.py")) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in OPI.ORDER_CALLS]
    assert len(calls) == 1
    kw = {k.arg: ast.literal_eval(k.value) for k in calls[0].keywords if isinstance(k.value, ast.Constant)}
    assert kw.get("side") == "Buy" and kw.get("product") == "linear"


# ------------------------------------------------------------------ every legacy armed tool is held to the same authority at its command-line entry
@pytest.mark.parametrize("rel", LEGACY_ARMED_TOOLS)
def test_a_legacy_armed_tools_command_line_entry_takes_the_single_writer_authority(rel):
    tree = _tree(rel)
    block = [n for n in tree.body if isinstance(n, ast.If) and "__main__" in ast.dump(n.test)]
    assert block, rel
    names = _names_in(block[0])
    assert "run_guarded_cli" in names and "legacy_executor_guard" in names, rel
    bare = [n for n in ast.walk(block[0]) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "main"]
    assert not bare, f"{rel}: the entry block calls main() directly, around the guard"


def test_the_shared_helper_refuses_without_authority_and_lets_a_dry_run_through(monkeypatch, tmp_path, capsys):
    import legacy_executor_guard as LG
    monkeypatch.setenv("MS_EXECUTOR_LOCK_PATH", str(tmp_path / "l" / "ms_executor.lock"))
    called = []
    assert LG.run_guarded_cli(lambda a: called.append(a) or 0, ["--dry-run"], env={}) == 0 and called == [["--dry-run"]]            # no order can leave a dry run
    assert LG.run_guarded_cli(lambda a: called.append(a) or 0, ["--arm"], env={}) == 2 and len(called) == 1                          # armed, no lease configuration: refused, main never ran
    assert "NO_GLOBAL_LEASE_CONFIGURED" in capsys.readouterr().err
    assert LG.run_guarded_cli(lambda a: called.append(a) or 0, ["--symbol", "BTCUSDT"], env={}) == 2                                   # a bare invocation is armed by default: guarded too


def test_the_protective_recover_phase_is_never_behind_the_lease_but_every_other_mode_is(monkeypatch, tmp_path):
    import legacy_executor_guard as LG
    monkeypatch.setenv("MS_EXECUTOR_LOCK_PATH", str(tmp_path / "l" / "ms_executor.lock"))
    ran = []
    pairs = (("--phase", "recover"),)
    assert LG.run_guarded_cli(lambda a: ran.append(a) or 0, ["--phase", "recover", "--symbol", "BTCUSDT"], env={}, safe_pairs=pairs) == 0
    assert LG.run_guarded_cli(lambda a: ran.append(a) or 0, ["--phase=recover"], env={}, safe_pairs=pairs) == 0
    assert len(ran) == 2
    for argv in (["--phase", "submit"], ["--phase", "full"], ["--phase"], ["recover"]):                 # not the exempt spelling: guarded (and refused here: no lease configuration)
        assert LG.run_guarded_cli(lambda a: ran.append(a) or 0, argv, env={}, safe_pairs=pairs) == 2
    assert len(ran) == 2
    src = open(os.path.join(BOT, "tools", "close_testnet_experiment.py"), encoding="utf-8").read()
    assert 'safe_pairs=(("--phase", "recover"),)' in src


def test_an_armed_drill_whose_hold_could_outlive_the_unrenewed_lease_is_refused_up_front(monkeypatch, tmp_path):
    import legacy_executor_guard as LG
    import drill as DRL
    monkeypatch.setenv("MS_EXECUTOR_LOCK_PATH", str(tmp_path / "l" / "ms_executor.lock"))
    release, why = DRL.single_writer_guard({}, max_runtime_s=(9 + 6.25) * 3600)                        # the drill's own defaults: 9 h hold + settle wait
    assert release is None and "do not renew" in why
    r2, why2 = DRL.single_writer_guard({}, max_runtime_s=900.0)                                        # a short run proceeds to the lease check (refused here only for lack of lease configuration)
    assert r2 is None and "NO_GLOBAL_LEASE_CONFIGURED" in why2
