"""The reviewer is a reader: schema, determinism, and the walls around it.

The boundary half (no venue imports, no `place_*`) lives in
`test_control_plane_boundary.py`, which was written before the tool existed
and stops skipping the moment it appears. What is here is everything else:
the emitted schema matches the charter, a dry run opens no socket, the key is
never printed, and the packaging story is stated rather than assumed.
"""
from __future__ import annotations

import ast
import datetime as dt
import json
import os
import re
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
REPO = os.path.dirname(BOT)
TOOL = os.path.join(BOT, "tools", "reviewer_verdict.py")

sys.path.insert(0, os.path.join(BOT, "tools"))

import reviewer_verdict as rv                         # noqa: E402

#: Distinguishes "the key is absent" from "the key is None".
_ABSENT = object()

SCHEMA = {"allows_progress", "blockers", "stage_b", "risk", "next_actions",
          "model", "key_status", "generated_utc"}


@pytest.fixture
def no_key(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)


# ---------------------------------------------------------------- schema ---

class TestSchema:
    def test_the_verdict_has_exactly_the_charter_keys(self, no_key):
        assert set(rv.build(BOT)) == SCHEMA

    def test_stage_b_reports_the_counter_the_gate_counts(self, no_key):
        stage = rv.build(BOT)["stage_b"]
        assert set(stage) == {"forward_n_trades", "of_20", "closed_forward_bars"}
        forward = json.load(open(
            os.path.join(BOT, "artifacts", "forward_shadow_current.json"),
            encoding="utf-8"))
        assert stage["forward_n_trades"] == forward["forward_n_trades"]
        assert stage["closed_forward_bars"] == forward["closed_forward_bars"]

    def test_risk_is_pinned_and_cannot_drift(self, no_key):
        assert rv.build(BOT)["risk"] == {"allows_live_must_be_false": True}

    def test_it_is_deterministic_apart_from_the_timestamp(self, no_key):
        first, second = rv.build(BOT), rv.build(BOT)
        first.pop("generated_utc"), second.pop("generated_utc")
        assert first == second


class TestTheRulesAreRulesNotOpinions:
    def test_a_missing_input_is_a_blocker_not_a_shrug(self, tmp_path, no_key):
        """Fail closed: no evidence is never 'looks fine'."""
        verdict = rv.build(str(tmp_path))
        assert verdict["allows_progress"] is False
        assert any(b.startswith("input missing") for b in verdict["blockers"])

    def test_below_twenty_trades_asks_for_accrual(self, no_key):
        verdict = rv.build(BOT)
        if verdict["stage_b"]["forward_n_trades"] < 20:
            assert any(a.startswith("accrue closed forward trades")
                       for a in verdict["next_actions"])

    def test_an_all_complete_gate_is_a_blocker(self, tmp_path, no_key):
        """A reviewer may not ratify live arming, however green the gate is."""
        art = tmp_path / "artifacts"
        art.mkdir()
        (art / "slice59_promotion_gate.json").write_text(json.dumps({
            "min_forward_trades": 20, "min_forward_days": 180,
            "checklist": {"a": {"complete": True, "owner": "human"},
                          "kill_switch_drill_recorded": {"complete": True,
                                                         "owner": "human"}},
        }), encoding="utf-8")
        verdict = rv.build(str(tmp_path))
        assert verdict["allows_progress"] is False
        assert any("a human must confirm live arming" in b
                   for b in verdict["blockers"])

    def test_an_incomplete_kill_drill_is_a_blocker(self, tmp_path, no_key):
        art = tmp_path / "artifacts"
        art.mkdir()
        (art / "slice59_promotion_gate.json").write_text(json.dumps({
            "checklist": {"kill_switch_drill_recorded": {"complete": False,
                                                         "owner": "human"}},
        }), encoding="utf-8")
        verdict = rv.build(str(tmp_path))
        assert any("kill_switch_drill_recorded is not complete" in b
                   for b in verdict["blockers"])

    def test_a_switch_left_engaged_is_a_blocker(self, tmp_path, no_key):
        art = tmp_path / "artifacts"
        art.mkdir()
        (art / "kill_switch_drill.json").write_text(
            json.dumps({"switch_engaged_at_exit": True}), encoding="utf-8")
        assert any("left ENGAGED" in b
                   for b in rv.build(str(tmp_path))["blockers"])

    def test_the_real_repo_currently_blocks_on_nothing_but_still_cannot_arm(self, no_key):
        """The live reading, asserted as a property rather than a number."""
        verdict = rv.build(BOT)
        assert verdict["risk"]["allows_live_must_be_false"] is True
        assert verdict["stage_b"]["forward_n_trades"] < verdict["stage_b"]["of_20"]

    def test_it_reads_the_gate_the_runtime_reads(self):
        """The defect this replaced: the reviewer globbed for the highest
        slice number and got slice76 — a snapshot frozen at 2 of 8 — then
        reported the human-signed kill-switch item as incomplete.

        The numbered gate files are history. Only one is the gate, and the
        authority on which is `promotion_gate.GATE_PATH`. The reviewer mirrors
        it as a constant rather than importing it, because it must stay
        stdlib-only; this test is what stops the two drifting apart. The test
        may import the runtime — the reviewer may not.
        """
        sys.path.insert(0, BOT)
        import promotion_gate                         # noqa: PLC0415
        assert os.path.realpath(rv.gate_path(BOT)) == \
            os.path.realpath(promotion_gate.GATE_PATH)

    def test_the_signed_kill_switch_item_is_not_reported_incomplete(self):
        """The false blocker, asserted directly against the real artifacts."""
        verdict = rv.build(BOT)
        assert not any("kill_switch_drill_recorded" in b
                       for b in verdict["blockers"]), verdict["blockers"]
        assert not any("kill_switch_drill_recorded" in a
                       for a in verdict["next_actions"])

    def test_a_snapshot_gate_is_never_mistaken_for_the_gate(self, tmp_path, no_key):
        """A higher-numbered file must not displace the real one."""
        art = tmp_path / "artifacts"
        art.mkdir()
        (art / "slice59_promotion_gate.json").write_text(json.dumps({
            "checklist": {"kill_switch_drill_recorded": {"complete": True,
                                                         "owner": "human"}},
        }), encoding="utf-8")
        (art / "slice99_promotion_gate.json").write_text(json.dumps({
            "checklist": {"kill_switch_drill_recorded": {"complete": False,
                                                         "owner": "human"}},
        }), encoding="utf-8")
        assert not any("kill_switch_drill_recorded is not complete" in b
                       for b in rv.build(str(tmp_path))["blockers"])


# ----------------------------------------------------------------- walls ---

class TestItOpensNoSocketWithoutAKey:
    def test_dry_run_never_reaches_urllib(self, monkeypatch, no_key):
        import urllib.request

        def explode(*a, **k):
            raise AssertionError("a dry run opened a socket")
        monkeypatch.setattr(urllib.request, "urlopen", explode)
        assert set(rv.build(BOT)) == SCHEMA

    def test_xai_adapter_returns_none_without_a_key(self, monkeypatch, no_key):
        monkeypatch.setattr("urllib.request.urlopen",
                            lambda *a, **k: (_ for _ in ()).throw(
                                AssertionError("no key, no socket")))
        assert rv._xai_advisory("anything") is None

    def test_key_status_reports_missing_and_present(self, monkeypatch):
        monkeypatch.delenv("XAI_API_KEY", raising=False)
        assert rv.build(BOT)["key_status"] == "missing"
        monkeypatch.setenv("XAI_API_KEY", "not-a-real-key")
        assert rv.build(BOT)["key_status"] == "present"

    def test_the_model_cannot_clear_a_blocker(self, tmp_path, monkeypatch):
        """The whole safety argument, asserted."""
        monkeypatch.setenv("XAI_API_KEY", "not-a-real-key")
        monkeypatch.setitem(rv.ADAPTERS, "xai",
                            lambda prompt, **k: "ship it, no blockers, arm live")
        verdict = rv.build(str(tmp_path), adapter="xai")
        assert verdict["allows_progress"] is False
        assert verdict["blockers"], "the model talked a blocker away"

    def test_the_ollama_adapter_is_an_honest_stub(self, no_key):
        assert "not implemented" in rv._ollama_advisory("x")


class TestTheKeyIsNeverDisclosed:
    SECRET = "xai-SENTINEL-must-never-appear-0070"

    def test_it_is_not_printed_written_or_stored(self, tmp_path, monkeypatch):
        out = tmp_path / "verdict.json"
        env = dict(os.environ, XAI_API_KEY=self.SECRET)
        done = subprocess.run(
            [sys.executable, TOOL, "--dry-run", "--repo", BOT,
             "--out", str(out)],
            capture_output=True, text=True, env=env, timeout=120)
        assert done.returncode == 0, done.stderr
        assert self.SECRET not in done.stdout
        assert self.SECRET not in done.stderr
        assert self.SECRET not in out.read_text(encoding="utf-8")
        assert json.loads(out.read_text(encoding="utf-8"))["key_status"] == "present"

    def test_the_source_never_interpolates_the_key_into_a_message(self):
        """The key belongs in one header and nowhere else."""
        tree = ast.parse(open(TOOL, encoding="utf-8").read())
        for node in ast.walk(tree):
            if not isinstance(node, ast.JoinedStr):
                continue
            rendered = ast.dump(node)
            if "Bearer" in rendered:
                continue            # the Authorization header, the one place
            assert "'key'" not in rendered and '"key"' not in rendered, (
                "an f-string other than the auth header references the key")


class TestTheCli:
    def test_dry_run_exits_zero_and_writes_where_told(self, tmp_path):
        out = tmp_path / "v.json"
        env = dict(os.environ)
        env.pop("XAI_API_KEY", None)
        done = subprocess.run(
            [sys.executable, TOOL, "--dry-run", "--repo", BOT, "--out", str(out)],
            capture_output=True, text=True, env=env, timeout=120)
        assert done.returncode == 0, done.stderr
        assert set(json.loads(out.read_text(encoding="utf-8"))) == SCHEMA
        assert "verdict written to" in done.stderr

    def test_it_does_not_write_into_artifacts_during_tests(self, tmp_path):
        """The 0060 lesson: three committed artefacts were clobbered by runs
        exactly like this one. The default is the real path, so every test
        passes --out."""
        source = open(TOOL, encoding="utf-8").read()
        assert 'default=None' in source
        assert "DEFAULT_OUT" in source

    def test_strict_turns_a_missing_input_into_exit_2(self, tmp_path):
        done = subprocess.run(
            [sys.executable, TOOL, "--dry-run", "--repo", str(tmp_path),
             "--out", str(tmp_path / "v.json"), "--strict"],
            capture_output=True, text=True, timeout=120)
        assert done.returncode == 2


class TestEvidenceThatStoppedMovingIsNotEvidence:
    """Both Stage B counters come from one file, and nothing asked its age.

    If `daily_forward_refresh` stopped — a broken venv, a cron dropped by an OS
    upgrade, a host that never came back from a reboot — the counters freeze and
    the reviewer goes on reporting "accrue closed forward trades: 3 of 20" with
    no blockers and allows_progress true, indefinitely. Stage B looks slow. It
    is stopped. Those are not the same finding and the verdict has to be able to
    tell them apart.
    """

    BASE = dt.datetime(2026, 9, 17, 0, 0, 0, tzinfo=dt.timezone.utc)

    def _repo(self, tmp_path, observed):
        art = tmp_path / "artifacts"
        art.mkdir(exist_ok=True)
        doc = {"forward_n_trades": 3, "closed_forward_bars": 36}
        if observed is not _ABSENT:
            doc["observed_at_utc"] = observed
        (art / "forward_shadow_current.json").write_text(json.dumps(doc))
        return str(tmp_path)

    #: The BASE instant, spelled the way the corpus tools write it.
    STAMP = "2026-09-17T00:00:00Z"

    def _findings(self, tmp_path, observed, hours_later):
        """Evaluate a repo whose shadow was observed at `observed`, as seen
        `hours_later` after BASE. The clock is injected so the thresholds can
        be tested at the boundary instead of waited out."""
        repo = self._repo(tmp_path, observed)
        return rv.evaluate(repo, now=self.BASE + dt.timedelta(hours=hours_later))

    def _stale_blockers(self, verdict):
        return [b for b in verdict["blockers"] if "forward shadow" in b]

    def _stale_actions(self, verdict):
        return [a for a in verdict["next_actions"] if "forward shadow" in a]

    def test_a_fresh_shadow_says_nothing_about_staleness(self, tmp_path, no_key):
        verdict = self._findings(tmp_path, self.STAMP, 2)
        assert self._stale_blockers(verdict) == []
        assert self._stale_actions(verdict) == []

    def test_a_missed_daily_refresh_is_a_next_action_not_a_blocker(
            self, tmp_path, no_key):
        """One missed run is worth saying. It is not worth halting over."""
        verdict = self._findings(tmp_path, self.STAMP, 30)
        assert self._stale_blockers(verdict) == []
        assert len(self._stale_actions(verdict)) == 1
        assert "30h old" in self._stale_actions(verdict)[0]
        assert "daily_forward_refresh" in self._stale_actions(verdict)[0]

    def test_two_missed_refreshes_block(self, tmp_path, no_key):
        verdict = self._findings(tmp_path, self.STAMP, 72)
        assert len(self._stale_blockers(verdict)) == 1
        assert "frozen, not slow" in self._stale_blockers(verdict)[0]
        assert "72h old" in self._stale_blockers(verdict)[0]

    def test_the_counters_are_still_reported_while_stale(self, tmp_path, no_key):
        """Refusing to trust a number is not a reason to stop showing it."""
        verdict = self._findings(tmp_path, self.STAMP, 72)
        assert verdict["stage_b"]["forward_n_trades"] == 3
        assert verdict["stage_b"]["closed_forward_bars"] == 36

    @pytest.mark.parametrize("hours,expect", [
        (25.9, "clean"), (26.1, "warn"), (47.9, "warn"), (48.1, "block"),
    ])
    def test_the_thresholds_are_where_they_say_they_are(self, tmp_path, no_key,
                                                        hours, expect):
        verdict = self._findings(tmp_path, self.STAMP, hours)
        got = ("block" if self._stale_blockers(verdict)
               else "warn" if self._stale_actions(verdict) else "clean")
        assert got == expect, (hours, verdict["blockers"],
                              verdict["next_actions"])

    def test_a_shadow_with_no_timestamp_is_a_blocker(self, tmp_path, no_key):
        """Fail closed. A counter you cannot age is a counter you cannot use."""
        verdict = self._findings(tmp_path, _ABSENT, 0)
        assert len(self._stale_blockers(verdict)) == 1
        assert "cannot be aged" in self._stale_blockers(verdict)[0]

    @pytest.mark.parametrize("junk", ["", "   ", "yesterday", "2026-13-45",
                                      None, 1758067200])
    def test_an_unparseable_timestamp_is_never_read_as_fresh(self, tmp_path,
                                                             no_key, junk):
        verdict = self._findings(tmp_path, junk, 0)
        assert len(self._stale_blockers(verdict)) == 1, junk

    def test_a_naive_timestamp_is_read_as_utc_not_rejected(self, tmp_path,
                                                           no_key):
        """The corpus tools write Z; a hand-edited file might not."""
        verdict = self._findings(tmp_path, "2026-09-17T00:00:00", 2)
        assert self._stale_blockers(verdict) == []
        assert self._stale_actions(verdict) == []

    def test_an_offset_timestamp_is_converted_not_assumed(self, tmp_path,
                                                          no_key):
        verdict = self._findings(tmp_path, "2026-09-16T20:00:00-04:00", 2)
        assert self._stale_blockers(verdict) == []

    def test_a_clock_that_is_behind_does_not_manufacture_staleness(
            self, tmp_path, no_key):
        """A shadow stamped in the future is odd, but it is not stale, and a
        negative age must not wrap into a blocker."""
        verdict = self._findings(tmp_path, self.STAMP, -5)
        assert self._stale_blockers(verdict) == []
        assert self._stale_actions(verdict) == []

    def test_on_the_real_repo_staleness_alone_is_enough_to_halt(self, no_key):
        """Everywhere else here the tmp repo is missing the drill and the gate,
        so `allows_progress` is already false and proves nothing. Against the
        shipped artifacts it is true today — so aging only the clock is the one
        variable, and it has to be enough on its own."""
        forward = json.load(open(
            os.path.join(BOT, "artifacts", "forward_shadow_current.json"),
            encoding="utf-8"))
        observed = dt.datetime.fromisoformat(
            forward["observed_at_utc"].replace("Z", "+00:00"))
        assert rv.evaluate(BOT, now=observed)["allows_progress"] is True
        aged = rv.evaluate(BOT, now=observed + dt.timedelta(hours=72))
        assert aged["allows_progress"] is False
        assert [b for b in aged["blockers"] if "forward shadow" in b]

    def test_the_shipped_shadow_is_not_stale_against_its_own_stamp(self):
        """Non-flaky liveness check: the rule must not fire on the real file
        read at the moment it was observed. Guards against shipping a threshold
        that blocks the repo the instant it lands."""
        forward = json.load(open(
            os.path.join(BOT, "artifacts", "forward_shadow_current.json"),
            encoding="utf-8"))
        observed = dt.datetime.fromisoformat(
            forward["observed_at_utc"].replace("Z", "+00:00"))
        verdict = rv.evaluate(BOT, now=observed)
        assert [b for b in verdict["blockers"] if "forward shadow" in b] == []


class TestItDoesNotDirtyTheRepoJustByRunning:
    """`artifacts/reviewer_verdict.json` is TRACKED, and the verdict is a pure
    function of tracked evidence plus a clock.

    So the runbook step `tools/reviewer_verdict.py --dry-run` used to leave a
    one-line diff behind every single time it was followed: generated_utc moved,
    nothing else did. Three runs in a shift is three reverts, or it is a person
    who has stopped reading `git status` — and `git status` is how the fixture-db
    guard and the verify receipt both get noticed.
    """

    def _run(self, out, *extra):
        """Pinned to the shipped shadow's own observed_at_utc, not the real
        clock: otherwise this whole class goes red the day the checked-in
        forward_shadow_current.json ages past the staleness threshold,
        which has nothing to do with what these tests are checking."""
        forward = json.load(open(
            os.path.join(BOT, "artifacts", "forward_shadow_current.json"),
            encoding="utf-8"))
        env = dict(os.environ)
        env.pop("XAI_API_KEY", None)
        return subprocess.run(
            [sys.executable, TOOL, "--dry-run", "--repo", BOT,
             "--out", str(out), "--now", forward["observed_at_utc"], *extra],
            capture_output=True, text=True, env=env, timeout=120)

    def test_a_second_identical_run_leaves_the_file_byte_identical(self, tmp_path):
        out = tmp_path / "v.json"
        assert self._run(out).returncode == 0
        first = out.read_bytes()
        assert self._run(out).returncode == 0
        assert out.read_bytes() == first

    def test_it_says_so_rather_than_pretending_it_wrote(self, tmp_path):
        out = tmp_path / "v.json"
        self._run(out)
        done = self._run(out)
        assert "verdict unchanged" in done.stderr
        assert "left alone" in done.stderr

    def test_the_verdict_it_prints_is_still_freshly_stamped(self, tmp_path):
        """Liveness has to stay visible somewhere, or a dead reviewer and a
        stable one look the same."""
        out = tmp_path / "v.json"
        self._run(out)
        onfile = json.loads(out.read_text(encoding="utf-8"))["generated_utc"]
        printed = json.loads(self._run(out).stdout)["generated_utc"]
        assert printed >= onfile
        assert json.loads(out.read_text(encoding="utf-8"))[
            "generated_utc"] == onfile

    def test_stamp_forces_the_write(self, tmp_path):
        out = tmp_path / "v.json"
        self._run(out)
        before = json.loads(out.read_text(encoding="utf-8"))["generated_utc"]
        done = self._run(out, "--stamp")
        assert "verdict written to" in done.stderr
        after = json.loads(out.read_text(encoding="utf-8"))["generated_utc"]
        assert after >= before

    def test_a_changed_finding_is_always_written(self, tmp_path):
        """Skipping the write must never skip a real change."""
        out = tmp_path / "v.json"
        self._run(out)
        doc = json.loads(out.read_text(encoding="utf-8"))
        doc["blockers"] = ["something a stale file would hide"]
        out.write_text(json.dumps(doc), encoding="utf-8")
        assert self._run(out).returncode == 0
        assert json.loads(out.read_text(encoding="utf-8"))["blockers"] == []

    def test_a_corrupt_existing_file_is_replaced_not_preserved(self, tmp_path):
        out = tmp_path / "v.json"
        out.write_text("{ this is not json", encoding="utf-8")
        assert self._run(out).returncode == 0
        assert set(json.loads(out.read_text(encoding="utf-8"))) == SCHEMA

    def test_a_first_run_into_a_missing_directory_still_works(self, tmp_path):
        out = tmp_path / "nested" / "deeper" / "v.json"
        assert self._run(out).returncode == 0
        assert out.is_file()

    def test_only_the_clock_is_ignored_in_the_comparison(self, tmp_path):
        """A comparison that ignored a second field would hide findings."""
        out = tmp_path / "v.json"
        self._run(out)
        baseline = json.loads(out.read_text(encoding="utf-8"))
        for key in SCHEMA - {"generated_utc"}:
            doc = dict(baseline)
            doc[key] = "MOVED"
            out.write_text(json.dumps(doc), encoding="utf-8")
            assert self._run(out).returncode == 0
            assert json.loads(out.read_text(encoding="utf-8"))[key] != "MOVED", (
                f"a change to {key} was treated as no change")


class TestPackaging:
    """Binary consistency, stated rather than left implicit.

    `test_image_ships_the_tools.py` asserts only that every tool deploy.sh
    NAMES is in the image; the inverse is deliberately not asserted, so a tool
    that is in neither list is consistent. `kill_switch_drill.py` is such a
    tool, and so is this one — but for the drill that was never written down,
    which is how a quiet drift becomes an argument later.
    """

    def _copied_tools(self):
        text = re.sub(r"\\\n", " ",
                      open(os.path.join(BOT, "Dockerfile"),
                           encoding="utf-8").read())
        out = set()
        for line in text.splitlines():
            if line.strip().startswith("COPY"):
                out.update(t for t in line.split()[1:-1]
                           if t.startswith("tools/") and t.endswith(".py"))
        return out

    def test_the_reviewer_is_not_in_the_image(self):
        assert "tools/reviewer_verdict.py" not in self._copied_tools()

    def test_and_no_deploy_instruction_promises_it(self):
        """Not shipped AND not promised = consistent. Either alone is a bug."""
        text = open(os.path.join(REPO, "scripts", "deploy.sh"),
                    encoding="utf-8").read()
        assert "reviewer_verdict" not in text

    def test_the_drill_precedent_still_holds(self):
        """If someone ships the drill, this pairing needs rethinking."""
        assert "tools/kill_switch_drill.py" not in self._copied_tools()

    def test_the_reviewer_imports_only_the_standard_library(self):
        """Why it needs no COPY: nothing in the image would be missing."""
        local = {f[:-3] for f in os.listdir(BOT) if f.endswith(".py")}
        tree = ast.parse(open(TOOL, encoding="utf-8").read())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert name.split(".")[0] not in local, (
                    f"the reviewer imports the bot module {name!r}; it is "
                    "supposed to read JSON, not link against the runtime")
