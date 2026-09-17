"""The orchestrator tick is glue, and glue is where an invariant leaks.

`test_control_plane_boundary.py` bans LLM clients from the order path and bans
the *reviewer* from the venue. The tick is the third body in that system: it is
the only thing that runs on a cron with no human watching, and it writes the
file the Builder then treats as orders. So the questions here are narrower and
meaner than "does it work":

* does it import anything on the order path (directly or by importlib)?
* can it set `allows_live`, clear the kill switch, or place an order?
* does it default to DRY RUN, so an unattended cron never writes corpus?
* does it actually regenerate NEXT_MISSION.md, rather than leaving a stale one
  that a Builder would read as current?
* does it leave `artifacts/forward_shadow_current.json` alone?

The behavioural half runs `main()` against a throwaway repo with `_run` stubbed,
so no child tool is spawned and the real artifacts/ is never touched. Stubbing
`_run` is also what makes the allows_live probe fail — and a failing probe MUST
still report False, which is the fail-closed case worth pinning.
"""
from __future__ import annotations

import ast
import inspect
import json
import os
import sys
import types

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
TOOL = os.path.join(BOT, "tools", "control_plane_tick.py")

sys.path.insert(0, os.path.join(BOT, "tools"))

import control_plane_tick as cpt                      # noqa: E402

#: Anything that can decide, size, gate or send an order. Same list as the
#: boundary test's ORDER_PATH, as module names rather than filenames.
ORDER_PATH_MODULES = {
    "main", "trading_engine", "carry_engine", "carry_broker", "carry_risk",
    "carry_costs", "bybit_connection", "risk_management", "position_sizing",
    "promotion_gate", "shadow_strategy", "shadow",
}

LLM_MODULES = {
    "openai", "anthropic", "claude", "grok", "xai", "ollama", "llama_cpp",
    "transformers", "langchain", "litellm", "cohere", "vertexai",
    "replicate", "huggingface_hub", "google",
}


def _tree():
    with open(TOOL, encoding="utf-8") as handle:
        return ast.parse(handle.read(), filename=TOOL)


def _imported_names(tree):
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _dynamic_import_targets(tree):
    out = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = (getattr(node.func, "attr", None)
                or getattr(node.func, "id", None))
        if name not in ("import_module", "__import__"):
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                out.add(arg.value)
    return out


# ------------------------------------------------------------- boundary ---

class TestTheTickImportsNothingOnTheOrderPath:
    def test_the_file_exists_where_the_cron_points(self):
        assert os.path.isfile(TOOL), TOOL

    def test_no_order_path_module_is_imported(self):
        tree = _tree()
        for name in _imported_names(tree) | _dynamic_import_targets(tree):
            root = name.split(".")[0]
            assert root not in ORDER_PATH_MODULES, (
                f"control_plane_tick imports {name!r}: the orchestrator reached "
                f"the order path. It must probe by subprocess, not by import")

    def test_no_model_client_is_imported(self):
        """The tick invokes the reviewer; it must not BE one."""
        tree = _tree()
        for name in _imported_names(tree) | _dynamic_import_targets(tree):
            assert name.split(".")[0].lower() not in LLM_MODULES, name

    def test_the_loaded_module_pulled_in_no_order_path_module(self):
        """Import-time proof, not just source-text proof.

        A transitive import through a helper would not show up in the AST of
        this one file. `promotion_gate` is reached by `python -c` subprocess in
        `_allows_live_status`, and that is the whole point of the design.

        Names, not modules, is the wrong test: the tick defines its own `main`,
        which collides with the order-path module `main.py`. So the assertion is
        that no order-path NAME is bound to a module object.
        """
        leaked = {name for name in ORDER_PATH_MODULES
                  if isinstance(vars(cpt).get(name), types.ModuleType)}
        assert not leaked, f"order-path modules bound in the tick: {leaked}"

    def test_it_is_stdlib_only(self):
        """A cron tool with third-party deps is a cron tool that stops running."""
        third_party = {n.split(".")[0] for n in _imported_names(_tree())}
        third_party -= set(sys.stdlib_module_names)
        third_party -= {"__future__"}
        assert not third_party, third_party


class TestTheTickCannotArmAnything:
    BANNED_CALLS = {"set_allows_live", "clear_kill_switch_by_human",
                    "promotion_gate_set_allows_live", "arm_live"}

    def test_it_calls_nothing_that_arms_live_or_clears_the_switch(self):
        for node in ast.walk(_tree()):
            if not isinstance(node, ast.Call):
                continue
            name = (getattr(node.func, "attr", None)
                    or getattr(node.func, "id", None) or "")
            assert not name.startswith("place_"), f"tick calls {name}()"
            assert name not in self.BANNED_CALLS, f"tick calls {name}()"

    def test_it_never_assigns_allows_live_a_true_value(self):
        """Reading allows_live is the job. Writing it is the crime."""
        for node in ast.walk(_tree()):
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            for target in targets:
                name = (getattr(target, "id", None)
                        or getattr(target, "attr", None))
                if name == "allows_live":
                    pytest.fail("tick assigns allows_live")

    def test_the_probe_declares_itself_read_only(self):
        probe = cpt._allows_live_status()
        assert probe["tick_may_set_allows_live"] is False
        assert isinstance(probe["allows_live"], bool)

    def test_a_broken_probe_fails_closed(self, monkeypatch):
        monkeypatch.setattr(cpt, "_run", lambda argv, **kw: {
            "returncode": 1, "stdout_tail": "", "error": "boom"})
        probe = cpt._allows_live_status()
        assert probe["allows_live"] is False
        assert probe["source"] == "fail_closed_default"

    def test_a_probe_that_claims_live_is_reported_not_obeyed(self, monkeypatch):
        """Even if the runtime says live, the tick only REPORTS it.

        The tick has no branch that behaves differently when live is true, and
        this pins that: the emitted risk block still says live must be false.
        """
        monkeypatch.setattr(cpt, "_run", lambda argv, **kw: {
            "returncode": 0,
            "stdout_tail": json.dumps({"allows_live": True}),
            "error": None})
        probe = cpt._allows_live_status()
        assert probe["allows_live"] is True
        assert probe["tick_may_set_allows_live"] is False


# ------------------------------------------------------------- dry run ----

class TestDryRunIsTheDefault:
    def test_run_tick_defaults_to_no_corpus_write(self):
        sig = inspect.signature(cpt.run_tick)
        assert sig.parameters["allow_append_write"].default is False

    def test_no_flag_means_no_write_flag_reaches_the_appender(self, recorder):
        cpt.run_tick()
        argv = recorder.argv_for("append_closed_corpus.py")
        assert "--write" not in argv, (
            "the unattended default must be a dry run; --write leaked in")

    def test_the_flag_is_the_only_way_to_write(self, recorder):
        cpt.run_tick(allow_append_write=True)
        assert "--write" in recorder.argv_for("append_closed_corpus.py")

    def test_the_env_opt_in_requires_exactly_one(self, monkeypatch, recorder,
                                                 tmp_repo):
        for value in ("", "0", "true", "yes", "2", " 1 x"):
            monkeypatch.setenv(cpt.APPEND_WRITE_ENV, value)
            recorder.calls.clear()
            assert cpt.main([]) == 0
            assert "--write" not in recorder.argv_for("append_closed_corpus.py"), (
                f"{value!r} was treated as opt-in")

        monkeypatch.setenv(cpt.APPEND_WRITE_ENV, "1")
        recorder.calls.clear()
        assert cpt.main([]) == 0
        assert "--write" in recorder.argv_for("append_closed_corpus.py")

    def test_the_tick_records_which_mode_it_ran_in(self, recorder):
        tick, _, _, _ = cpt.run_tick()
        assert tick["append_write_enabled"] is False
        tick, _, _, _ = cpt.run_tick(allow_append_write=True)
        assert tick["append_write_enabled"] is True

    def test_the_reviewer_is_invoked_dry_run(self, recorder):
        cpt.run_tick()
        argv = recorder.argv_for("reviewer_verdict.py")
        assert "--dry-run" in argv

    def test_no_key_means_no_xai_call(self, monkeypatch, recorder):
        monkeypatch.delenv("XAI_API_KEY", raising=False)
        _, _, _, _ = cpt.run_tick()
        assert not [c for c in recorder.calls if "--xai" in c]


# -------------------------------------------------------- mission output ---

class TestItRegeneratesTheMission:
    def test_main_writes_both_artefacts(self, tmp_repo, recorder):
        assert cpt.main([]) == 0
        assert os.path.isfile(os.path.join(tmp_repo, cpt.MISSION_OUT))
        assert os.path.isfile(os.path.join(tmp_repo, cpt.TICK_OUT))

    def test_a_stale_mission_is_overwritten_not_appended(self, tmp_repo,
                                                         recorder):
        """A Builder reading a stale mission is the failure this prevents."""
        path = os.path.join(tmp_repo, cpt.MISSION_OUT)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("STALE ORDERS: buy everything\n")
        assert cpt.main([]) == 0
        body = open(path, encoding="utf-8").read()
        assert "STALE ORDERS" not in body
        assert body.startswith("# NEXT_MISSION")

    def test_the_mission_carries_the_counters_the_gate_counts(self, tmp_repo,
                                                              recorder):
        assert cpt.main([]) == 0
        body = open(os.path.join(tmp_repo, cpt.MISSION_OUT),
                    encoding="utf-8").read()
        assert "stage_b: forward_n_trades=7/20" in body
        assert "closed_forward_bars=41/180" in body

    def test_the_mission_states_live_is_off_and_unflippable(self, tmp_repo,
                                                           recorder):
        assert cpt.main([]) == 0
        body = open(os.path.join(tmp_repo, cpt.MISSION_OUT),
                    encoding="utf-8").read()
        assert "allows_live: false (tick MUST NOT flip this)" in body
        assert "- set allows_live / clear kill switch / place orders" in body

    def test_human_gates_are_quarantined_from_the_do_list(self, tmp_repo,
                                                          recorder):
        """The Builder must never find a human signature in its DO block."""
        assert cpt.main([]) == 0
        body = open(os.path.join(tmp_repo, cpt.MISSION_OUT),
                    encoding="utf-8").read()
        do_block = body.split("## DO", 1)[1].split("## HUMAN-ONLY", 1)[0]
        human_block = body.split("## HUMAN-ONLY", 1)[1].split("## BLOCKERS", 1)[0]
        assert "human_risk_memo_signed" not in do_block
        assert "human_risk_memo_signed" in human_block

    def test_a_missing_verdict_produces_a_fail_closed_mission(self, tmp_repo,
                                                              recorder):
        os.remove(os.path.join(tmp_repo, cpt.VERDICT_OUT))
        assert cpt.main([]) == 0
        body = open(os.path.join(tmp_repo, cpt.MISSION_OUT),
                    encoding="utf-8").read()
        assert "allows_progress: false" in body
        assert "## BLOCKERS" in body
        assert "- (none)" not in body.split("## BLOCKERS", 1)[1]

    def test_the_tick_json_pins_the_risk_block(self, tmp_repo, recorder):
        assert cpt.main([]) == 0
        tick = json.load(open(os.path.join(tmp_repo, cpt.TICK_OUT),
                              encoding="utf-8"))
        assert tick["risk"] == {
            "allows_live_must_be_false": True,
            "tick_sets_allows_live": False,
            "tick_places_orders": False,
            "secrets_printed": False,
        }
        assert tick["allows_live"] is False
        assert tick["forward_shadow_write"] is False

    def test_it_leaves_the_forward_shadow_alone(self, tmp_repo, recorder):
        path = os.path.join(tmp_repo, cpt.FORWARD)
        before = open(path, "rb").read()
        assert cpt.main([]) == 0
        assert open(path, "rb").read() == before

    def test_the_artefact_carries_no_child_stdout(self, tmp_repo, recorder):
        """Tails are where a key would surface. They are dropped on purpose."""
        assert cpt.main([]) == 0
        raw = open(os.path.join(tmp_repo, cpt.TICK_OUT), encoding="utf-8").read()
        assert "stdout_tail" not in raw
        assert "stderr_tail" not in raw
        assert "step_tails" not in raw

    def test_the_summary_it_prints_carries_no_key(self, tmp_repo, recorder,
                                                  monkeypatch, capsys):
        monkeypatch.setenv("XAI_API_KEY", "xai-SECRETVALUE-do-not-print")
        assert cpt.main([]) == 0
        captured = capsys.readouterr()
        assert "SECRETVALUE" not in captured.out
        assert "SECRETVALUE" not in captured.err


# ------------------------------------------------------------- fixtures ---

class _Recorder:
    """Stands in for subprocess. Records argv, spawns nothing."""

    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        return {
            "argv": list(argv),
            "started_utc": "2026-01-01T00:00:00Z",
            "ended_utc": "2026-01-01T00:00:00Z",
            "returncode": 0,
            "stdout_tail": "",
            "stderr_tail": "",
            "error": None,
        }

    def argv_for(self, needle):
        for call in self.calls:
            if any(needle in part for part in call):
                return call
        raise AssertionError(f"no child invocation matched {needle!r}: "
                             f"{self.calls}")


VERDICT = {
    "allows_progress": True,
    "blockers": [],
    "stage_b": {"forward_n_trades": 7, "of_20": 20, "closed_forward_bars": 41},
    "risk": {"allows_live_must_be_false": True},
    "next_actions": [
        "accrue closed forward trades: 7 of 20",
        "gate item open (observation): forward_shadow_clean",
        "gate item open (human): human_risk_memo_signed",
        "ADVISORY: reviewer offline",
    ],
    "model": "none (local rules only)",
    "key_status": "missing",
    "generated_utc": "2026-01-01T00:00:00Z",
}


@pytest.fixture
def tmp_repo(tmp_path, monkeypatch):
    """A throwaway bot/ so no test can dirty the real artifacts/."""
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "reviewer_verdict.json").write_text(json.dumps(VERDICT))
    (artifacts / "forward_shadow_current.json").write_text(
        json.dumps({"forward_n_trades": 7, "closed_forward_bars": 41}))
    monkeypatch.setattr(cpt, "REPO", str(tmp_path))
    return str(tmp_path)


@pytest.fixture
def recorder(tmp_repo, monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(cpt, "_run", rec)
    return rec
