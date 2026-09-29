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
import subprocess
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

class TestTheTickHasNoWriteAuthorityAtAll:
    """The tick used to carry an opt-in (CONTROL_PLANE_ALLOW_APPEND_WRITE=1 /
    --allow-append-write) that passed --write through to append_closed_corpus.py.
    That opt-in has been REMOVED, not merely left at its off default — the
    tick is orchestrator/observer only; the sanctioned corpus-write path is
    exclusively bot/scripts/stage_b_forward_accrual.sh on the Factory Mac's
    cron (docs/human/NO_GLUE_OPS.md item 10). These tests prove there is no
    way left, including stale environment state from before the removal, to
    make the tick pass --write to the appender."""

    def test_run_tick_takes_no_write_related_parameter(self):
        """The signature itself must not offer a way to ask for a write —
        not just default to False, but not exist as a knob at all."""
        sig = inspect.signature(cpt.run_tick)
        assert "allow_append_write" not in sig.parameters
        assert list(sig.parameters) == [], (
            "run_tick must take no parameters — nothing configures whether "
            "it writes, because it never does")

    def test_no_write_flag_ever_reaches_the_appender(self, recorder):
        cpt.run_tick()
        argv = recorder.argv_for("append_closed_corpus.py")
        assert "--write" not in argv

    def test_main_takes_no_allow_append_write_flag(self):
        """The CLI surface itself must not offer the flag — argparse must
        reject it outright, not silently accept and ignore it."""
        with pytest.raises(SystemExit):
            cpt.main(["--allow-append-write"])

    def test_stale_env_var_from_before_the_removal_has_no_effect(
            self, monkeypatch, recorder, tmp_repo):
        """A host whose crontab or shell profile still exports
        CONTROL_PLANE_ALLOW_APPEND_WRITE=1 from before this opt-in was
        removed must not silently regain write authority."""
        for value in ("", "0", "1", "true", "yes"):
            monkeypatch.setenv(cpt.APPEND_WRITE_ENV, value)
            recorder.calls.clear()
            assert cpt.main([]) == 0
            assert "--write" not in recorder.argv_for("append_closed_corpus.py"), (
                f"stale env value {value!r} leaked --write through")

    def test_the_tick_records_append_write_as_permanently_false(self, recorder):
        tick, _, _, _ = cpt.run_tick()
        assert tick["append_write_enabled"] is False

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

    def test_a_missing_verdict_resolves_bars_from_a_nested_shadow_file(
            self, tmp_repo, recorder):
        """A freshly-promoted shadow file carries `ceiling.closed_forward_bars`,
        not a top-level `closed_forward_bars` key. When the verdict is also
        missing, the fallback stage_b block built straight from the shadow
        file must still resolve the real count (43), not report 0."""
        os.remove(os.path.join(tmp_repo, cpt.VERDICT_OUT))
        shadow_path = os.path.join(tmp_repo, "artifacts",
                                   "forward_shadow_current.json")
        with open(shadow_path, "w", encoding="utf-8") as handle:
            json.dump({
                "forward_n_trades": 3,
                "ceiling": {"closed_forward_bars": 43},
            }, handle)

        assert cpt.main([]) == 0

        body = open(os.path.join(tmp_repo, cpt.MISSION_OUT),
                    encoding="utf-8").read()
        assert "closed_forward_bars=43/180" in body

        tick = json.load(open(os.path.join(tmp_repo, cpt.TICK_OUT),
                              encoding="utf-8"))
        assert tick["closed_forward_bars"] == 43

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


# ------------------------------------------------------- the loop wrapper ---

class TestTheLoopWrapperDefaultsToReturning:
    """`MODE="${1:-loop}"` made the never-ending mode the default one.

    A cron line that forgot the argument would start a fresh daemon on every
    firing, and the way you would find out is the process table. The default is
    now `once`: wrong only in the direction where something stops happening.
    """

    SCRIPT = os.path.join(BOT, "tools", "control_plane_loop.sh")

    @pytest.fixture
    def sandbox(self, tmp_path):
        """A throwaway bot/ whose tick just leaves a mark and exits."""
        tools = tmp_path / "tools"
        tools.mkdir()
        (tmp_path / "artifacts").mkdir()
        marker = tmp_path / "ticks.txt"
        (tools / "control_plane_tick.py").write_text(
            "open(%r, 'a').write('tick\\n')\n" % str(marker))
        script = tools / "control_plane_loop.sh"
        script.write_bytes(open(self.SCRIPT, "rb").read())
        script.chmod(0o755)

        def run(*args, timeout=20):
            return subprocess.run([str(script), *args], capture_output=True,
                                  text=True, timeout=timeout)

        return types.SimpleNamespace(run=run, marker=marker, root=tmp_path)

    def test_no_arguments_runs_one_tick_and_exits(self, sandbox):
        done = sandbox.run(timeout=20)
        assert done.returncode == 0, done.stderr
        assert sandbox.marker.read_text().count("tick") == 1

    def test_once_is_explicit_and_identical(self, sandbox):
        assert sandbox.run("once").returncode == 0
        assert sandbox.marker.read_text().count("tick") == 1

    def test_the_daemon_is_still_one_word_away(self, sandbox):
        """Making the safe thing default must not remove the useful thing."""
        with pytest.raises(subprocess.TimeoutExpired):
            sandbox.run("loop", "1", timeout=4)
        assert sandbox.marker.read_text().count("tick") >= 1

    def test_a_nonsense_interval_is_refused_rather_than_defaulted(self, sandbox):
        done = sandbox.run("loop", "0")
        assert done.returncode == 2
        done = sandbox.run("loop", "abc")
        assert done.returncode == 2

    def test_an_unknown_mode_prints_usage(self, sandbox):
        done = sandbox.run("sometimes")
        assert done.returncode == 2
        assert "usage:" in done.stderr

    def test_it_logs_every_run_it_makes(self, sandbox):
        sandbox.run()
        log = (sandbox.root / "artifacts" / "control_plane_loop.log").read_text()
        assert "control_plane_loop start" in log
        assert "control_plane_loop end" in log


# ----------------------------------------------------------------- lock ---

class TestOnlyOneTickRunsAtATime:
    """The cron fires hourly; the tick has a 600s child and a 180s child.

    Nothing guarantees a tick finishes inside the hour, and two overlapping
    ticks means two dry-run appenders and two reviewer/tick-artefact writers
    racing against the same files — the tick itself has no corpus-write
    authority any more, but the artefact writes are still worth serializing.
    """

    def _lockdir(self, tmp_repo):
        return os.path.join(tmp_repo, cpt.LOCK_DIR)

    def test_the_lock_lives_on_ignored_ground(self):
        """A lock directory under artifacts/ would show up as a dirty tree."""
        assert cpt.LOCK_DIR.split(os.sep)[0] == "state"

    def test_a_tick_takes_the_lock_and_gives_it_back(self, tmp_repo, recorder):
        seen = {}

        real = cpt.run_tick

        def watching(**kwargs):
            seen["held"] = os.path.isdir(self._lockdir(tmp_repo))
            return real(**kwargs)

        monkey = pytest.MonkeyPatch()
        monkey.setattr(cpt, "run_tick", watching)
        try:
            assert cpt.main([]) == 0
        finally:
            monkey.undo()
        assert seen["held"] is True, "the tick ran without holding the lock"
        assert not os.path.isdir(self._lockdir(tmp_repo)), "lock not released"

    def test_a_second_tick_skips_while_the_first_holds_it(self, tmp_repo,
                                                          recorder, capsys):
        held, path = cpt._lock()
        assert held
        try:
            mission = os.path.join(tmp_repo, cpt.MISSION_OUT)
            os.makedirs(os.path.dirname(mission), exist_ok=True)
            with open(mission, "w", encoding="utf-8") as handle:
                handle.write("WRITTEN BY THE FIRST TICK\n")
            assert cpt.main([]) == 0, "an overlap is not a fault"
            assert "another tick is running" in capsys.readouterr().out
            assert open(mission, encoding="utf-8").read() == (
                "WRITTEN BY THE FIRST TICK\n")
        finally:
            cpt._unlock(path)

    def test_the_skip_spawns_no_child(self, tmp_repo, recorder):
        held, path = cpt._lock()
        try:
            recorder.calls.clear()
            assert cpt.main([]) == 0
            assert recorder.calls == [], (
                "the skipped tick still ran the corpus tools")
        finally:
            cpt._unlock(path)

    def test_a_lock_whose_owner_died_is_taken(self, tmp_repo, recorder):
        """A tick killed by a reboot must not wedge the control plane."""
        dead = subprocess.Popen([sys.executable, "-c", ""])
        dead.wait()
        lock = self._lockdir(tmp_repo)
        os.makedirs(lock)
        with open(os.path.join(lock, "pid"), "w", encoding="utf-8") as handle:
            handle.write(f"{dead.pid}\n")
        held, path = cpt._lock()
        assert held, "a dead owner's lock was treated as held"
        cpt._unlock(path)

    def test_a_lock_with_an_unreadable_owner_is_never_stolen(self, tmp_repo):
        """Stealing on a guess is how you get the overlap back."""
        lock = self._lockdir(tmp_repo)
        os.makedirs(lock)
        held, why = cpt._lock()
        assert held is False
        assert "held by pid" in why
        assert os.path.isdir(lock), "the lock was removed anyway"

    def test_a_garbled_pid_is_treated_as_alive(self, tmp_repo):
        lock = self._lockdir(tmp_repo)
        os.makedirs(lock)
        with open(os.path.join(lock, "pid"), "w", encoding="utf-8") as handle:
            handle.write("not-a-pid\n")
        held, _ = cpt._lock()
        assert held is False

    def test_the_lock_is_released_when_the_tick_raises(self, tmp_repo,
                                                       recorder):
        """`finally`, not "and then". A crash must not need a human to clear it."""
        monkey = pytest.MonkeyPatch()
        monkey.setattr(cpt, "run_tick",
                       lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
        try:
            with pytest.raises(RuntimeError):
                cpt.main([])
        finally:
            monkey.undo()
        assert not os.path.isdir(self._lockdir(tmp_repo))

    def test_the_owner_recorded_is_this_process(self, tmp_repo):
        held, path = cpt._lock()
        try:
            assert held
            with open(os.path.join(path, "pid"), encoding="utf-8") as handle:
                assert handle.read().strip() == str(os.getpid())
        finally:
            cpt._unlock(path)


# ------------------------------------------------- cross-machine bridge ---

class TestTheMissionDeliveryIsRecordedHonestly:
    """FAILS CLOSED, the way TestTheReviewerCannotReachTheVenue does.

    MACHINE A's generated AGENTS.md opens by reading NEXT_MISSION.md "via the
    clone". MACHINE B writes that file hourly and nothing commits or pushes it,
    so the clone never receives it. Either a writer exists and the mission is
    tracked, or the charter says in writing that the bridge is a dead drop.
    What must not happen is the third case: no writer, and nothing saying so.
    """

    CHARTER = os.path.join(BOT, "docs", "human", "AGENT_CONTROL_PLANE.md")
    SEARCH = (os.path.join(BOT, "tools"),
              os.path.join(os.path.dirname(BOT), "scripts"))

    def _writers(self):
        """Files with a `git commit` or `git push` in command position."""
        found = []
        for root in self.SEARCH:
            for base, _dirs, files in os.walk(root):
                for name in files:
                    if not name.endswith((".py", ".sh")):
                        continue
                    path = os.path.join(base, name)
                    with open(path, encoding="utf-8", errors="replace") as fh:
                        for lineno, line in enumerate(fh, 1):
                            body = line.strip().lstrip('"\'(')
                            if body.startswith(("git commit", "git push")):
                                found.append(f"{name}:{lineno}")
        return found

    def _mission_is_tracked(self):
        done = subprocess.run(
            ["git", "ls-files", "--error-unmatch",
             os.path.join("bot", cpt.MISSION_OUT)],
            cwd=os.path.dirname(BOT), capture_output=True, text=True)
        return done.returncode == 0

    def test_either_a_writer_exists_or_the_charter_says_it_does_not(self):
        writers = self._writers()
        if writers:
            assert self._mission_is_tracked(), (
                f"something now pushes ({writers}) but the mission is still "
                f"untracked, so the clone still gets nothing")
            return
        body = open(self.CHARTER, encoding="utf-8").read()
        assert "NEXT_MISSION has no writer" in body, (
            "no writer, and the charter does not record the gap")
        assert "dead drop" in body

    def test_the_gitignore_and_the_charter_agree(self):
        """Two places can disagree; this is the one that notices."""
        if self._writers():
            pytest.skip("a writer appeared; the test above owns that case")
        ignore = open(os.path.join(BOT, ".gitignore"), encoding="utf-8").read()
        assert "artifacts/NEXT_MISSION.md" in ignore
        assert not self._mission_is_tracked(), (
            "the mission is tracked while the charter says it is not delivered")

    def test_the_charter_does_not_pretend_the_bridge_works(self):
        body = open(self.CHARTER, encoding="utf-8").read()
        section = body.split("NEXT_MISSION has no writer", 1)[1]
        section = section.split("\n---", 1)[0]
        # Both remedies named, neither claimed as done.
        assert "Push side" in section and "Pull side" in section
        assert "neither taken here" in section


# ---------------------------------------------------- verdict promotion ---

class TestTheVerdictIsPromotedNotStamped:
    """The tracked verdict must not change when only the clock did.

    The reviewer recomputes the same finding from the same tracked evidence
    every hour and stamps a fresh `generated_utc`. Writing that straight into
    artifacts/ left the working tree dirty on a permanent loop — `git status`
    never clean, `git checkout` refusing to switch branches, and a genuine move
    in `blockers` buried in timestamp noise.
    """

    def _scratch(self, tmp_repo, doc):
        path = os.path.join(tmp_repo, cpt.VERDICT_SCRATCH)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(doc, handle)
        return path

    def test_the_reviewer_scores_to_scratch_not_to_artifacts(self, recorder):
        cpt.run_tick()
        argv = recorder.argv_for("reviewer_verdict.py")
        assert cpt.VERDICT_SCRATCH in argv
        assert cpt.VERDICT_OUT not in argv

    def test_the_scratch_path_is_not_tracked_ground(self):
        """state/ is gitignored; artifacts/ is not. That is the whole trick."""
        assert cpt.VERDICT_SCRATCH.split(os.sep)[0] == "state"
        assert cpt.VERDICT_OUT.split(os.sep)[0] == "artifacts"

    def test_a_new_timestamp_alone_does_not_touch_the_tracked_file(
            self, tmp_repo, recorder):
        tracked = os.path.join(tmp_repo, cpt.VERDICT_OUT)
        before = open(tracked, "rb").read()
        self._scratch(tmp_repo, dict(VERDICT,
                                     generated_utc="2099-12-31T23:59:59Z"))
        assert cpt.main([]) == 0
        assert open(tracked, "rb").read() == before

    def test_a_changed_finding_is_promoted(self, tmp_repo, recorder):
        moved = dict(VERDICT, allows_progress=False,
                     blockers=["forward shadow went stale"],
                     generated_utc="2099-12-31T23:59:59Z")
        self._scratch(tmp_repo, moved)
        assert cpt.main([]) == 0
        now = json.load(open(os.path.join(tmp_repo, cpt.VERDICT_OUT),
                             encoding="utf-8"))
        assert now["blockers"] == ["forward shadow went stale"]
        assert now["generated_utc"] == "2099-12-31T23:59:59Z"

    def test_a_promoted_blocker_reaches_the_mission(self, tmp_repo, recorder):
        """Promotion that the Builder never sees is promotion that did nothing."""
        self._scratch(tmp_repo, dict(VERDICT, allows_progress=False,
                                     blockers=["forward shadow went stale"]))
        assert cpt.main([]) == 0
        body = open(os.path.join(tmp_repo, cpt.MISSION_OUT),
                    encoding="utf-8").read()
        assert "forward shadow went stale" in body
        assert "allows_progress: false" in body

    def test_a_first_run_with_no_tracked_verdict_promotes(self, tmp_repo,
                                                          recorder):
        os.remove(os.path.join(tmp_repo, cpt.VERDICT_OUT))
        self._scratch(tmp_repo, VERDICT)
        assert cpt.main([]) == 0
        assert os.path.isfile(os.path.join(tmp_repo, cpt.VERDICT_OUT))

    def test_a_missing_scratch_leaves_the_tracked_verdict_alone(
            self, tmp_repo, recorder):
        """The reviewer failing must not blank the last known finding."""
        tracked = os.path.join(tmp_repo, cpt.VERDICT_OUT)
        before = open(tracked, "rb").read()
        assert cpt.main([]) == 0
        assert open(tracked, "rb").read() == before
        tick = json.load(open(os.path.join(tmp_repo, cpt.TICK_OUT),
                              encoding="utf-8"))
        assert tick["steps"]["reviewer_verdict_promote"]["promoted"] is False

    def test_liveness_is_still_reported_every_tick(self, tmp_repo, recorder):
        """`generated_utc` now means "when the finding changed". Something must
        still say "the reviewer ran just now", or a dead cron looks identical
        to a stable verdict."""
        self._scratch(tmp_repo, dict(VERDICT,
                                     generated_utc="2099-12-31T23:59:59Z"))
        assert cpt.main([]) == 0
        reviewer = json.load(open(os.path.join(tmp_repo, cpt.TICK_OUT),
                                  encoding="utf-8"))["reviewer"]
        assert reviewer["promoted"] is False
        assert reviewer["computed_utc"] == "2099-12-31T23:59:59Z"
        assert reviewer["generated_utc"] == VERDICT["generated_utc"]

    def test_a_failed_reviewer_does_not_promote_yesterdays_scratch(
            self, tmp_repo, recorder):
        """The scratch file outlives the run that wrote it.

        If this tick's reviewer died, the previous tick's scratch is still on
        disk. A promote that only asked "is the file readable" would copy that
        stale finding into artifacts/ and present it as current.
        """
        tracked = os.path.join(tmp_repo, cpt.VERDICT_OUT)
        before = open(tracked, "rb").read()
        self._scratch(tmp_repo, dict(VERDICT, allows_progress=False,
                                     blockers=["stale from yesterday"]))
        recorder.fail["reviewer_verdict.py"] = 1
        assert cpt.main([]) == 0
        assert open(tracked, "rb").read() == before
        promote = json.load(open(os.path.join(tmp_repo, cpt.TICK_OUT),
                                 encoding="utf-8"))["steps"][
                                     "reviewer_verdict_promote"]
        assert promote["promoted"] is False
        assert promote["reason"] == "reviewer did not succeed"

    def test_the_failure_is_still_recorded_as_an_error(self, tmp_repo,
                                                       recorder):
        """Refusing to promote must not make the failure invisible."""
        recorder.fail["reviewer_verdict.py"] = 1
        assert cpt.main([]) == 0
        tick = json.load(open(os.path.join(tmp_repo, cpt.TICK_OUT),
                              encoding="utf-8"))
        assert any("reviewer_verdict" in e for e in tick["errors"]), tick["errors"]

    def test_with_a_key_the_xai_run_is_the_one_that_must_succeed(
            self, tmp_repo, recorder, monkeypatch):
        """--xai overwrites the scratch last, so its rc is the one that counts.

        A dry run that succeeded before a failed --xai would otherwise be read
        as permission to promote whatever the broken run left behind.
        """
        monkeypatch.setenv("XAI_API_KEY", "present")
        tracked = os.path.join(tmp_repo, cpt.VERDICT_OUT)
        before = open(tracked, "rb").read()
        self._scratch(tmp_repo, dict(VERDICT, blockers=["half-written"]))
        recorder.fail["--xai"] = 1
        assert cpt.main([]) == 0
        assert open(tracked, "rb").read() == before
        promote = json.load(open(os.path.join(tmp_repo, cpt.TICK_OUT),
                                 encoding="utf-8"))["steps"][
                                     "reviewer_verdict_promote"]
        assert promote["reason"] == "reviewer did not succeed"

    def test_without_a_key_a_green_dry_run_is_enough(self, tmp_repo, recorder,
                                                     monkeypatch):
        monkeypatch.delenv("XAI_API_KEY", raising=False)
        self._scratch(tmp_repo, dict(VERDICT, blockers=["a real new blocker"]))
        assert cpt.main([]) == 0
        now = json.load(open(os.path.join(tmp_repo, cpt.VERDICT_OUT),
                             encoding="utf-8"))
        assert now["blockers"] == ["a real new blocker"]

    def test_substance_ignores_only_the_clock(self):
        """A comparison that ignored too much would silently drop findings."""
        base = dict(VERDICT)
        assert cpt._substance(base) == cpt._substance(
            dict(base, generated_utc="different"))
        for key in ("allows_progress", "blockers", "stage_b", "next_actions",
                    "risk", "model", "key_status"):
            changed = dict(base)
            changed[key] = "MOVED"
            assert cpt._substance(base) != cpt._substance(changed), key


# ------------------------------------------------------------- fixtures ---

class _Recorder:
    """Stands in for subprocess. Records argv, spawns nothing.

    `fail` maps a substring of a child's argv to the returncode it should
    report, so a test can kill one tool and leave the rest working.
    """

    def __init__(self):
        self.calls = []
        self.fail = {}

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        joined = " ".join(argv)
        rc = 0
        for needle, code in self.fail.items():
            if needle in joined:
                rc = code
                break
        return {
            "argv": list(argv),
            "started_utc": "2026-01-01T00:00:00Z",
            "ended_utc": "2026-01-01T00:00:00Z",
            "returncode": rc,
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
