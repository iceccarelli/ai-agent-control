"""0042 — the image does not build, and the runtime that would have run it.

THE BUG THIS FILE EXISTS FOR
============================
`.dockerignore` excludes `tools/` with no negation. The Dockerfile says:

    COPY tools/connector_check.py tools/session_tail.py ./tools/

Docker filters the build CONTEXT before the Dockerfile runs, so those two files
were never sent to the daemon and that COPY fails. The image has never built.
`tests/test_dockerfile.py` checked the import closure — the right idea against
the wrong artefact: it proved the COPY LIST was complete and never asked
whether the context could deliver it.

Every deployment in this repo pointed at that image. 0038's ECS task
definition, its one-off `connector_check` task, the whole Phase D drill.

TWO MACHINES IS TWO BOOKS
=========================
The rest of this file is about the runtime. On Fly the default deploy strategy
is `rolling`, and `bluegreen` and `canary` boot a new machine ALONGSIDE the old
one. For a web app that is the point. For this, a second process means a second
`CarryEngine`, a second cold start, and a second $100 hedge against inventory
that only backs one — while the first machine's ledger says everything is fine.

`auto_stop_machines` is the same hazard from the other end: a book holding a
hedge that Fly stops because nothing is hitting an HTTP port is a naked client.
"""
from __future__ import annotations

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import fly_stack  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestTheBuildContextCanDeliverEveryCopy:
    """The test that was missing. It reimplements Docker's context filter and
    asks the only question that matters: does every COPY source survive it?"""

    def test_every_copy_source_survives_the_dockerignore(self):
        missing = fly_stack.context_conflicts(ROOT)
        assert missing == [], (
            "these Dockerfile COPY sources are excluded from the build "
            f"context by .dockerignore, so the image cannot build: {missing}")

    def test_the_filter_actually_catches_the_bug_it_was_written_for(self):
        """A checker that cannot fail is not a checker. This feeds it the
        exact .dockerignore that shipped and demands a complaint."""
        assert fly_stack.excluded_by(["tools/"], "tools/connector_check.py")
        assert fly_stack.excluded_by(["state/"], "state/trading_state.db")
        assert not fly_stack.excluded_by(["tools/"], "toolsmith.py")
        assert not fly_stack.excluded_by(["*.md"], "main.py")
        assert fly_stack.excluded_by(["*.md"], "README.md")

    def test_a_negation_re_includes(self):
        assert not fly_stack.excluded_by(
            ["tools/*", "!tools/connector_check.py"],
            "tools/connector_check.py")

    def test_no_wildcard_directory_copy_smuggles_the_operator_scripts(self):
        """`tools/` is no longer excluded by .dockerignore, because a
        re-inclusion whose semantics this repo cannot test is not a control.
        What replaces it is static and testable: the Dockerfile must name
        every tools file one by one."""
        text = re.sub(r"\\\n", " ",
                      open(os.path.join(ROOT, "Dockerfile"),
                           encoding="utf-8").read())
        for line in text.splitlines():
            if not line.strip().startswith("COPY"):
                continue
            for token in line.split()[1:-1]:
                assert token.rstrip("/") not in ("tools", "tests", "data",
                                                 "docs", "."), \
                    f"COPY {token} would ship a whole directory"

    def test_secrets_are_still_excluded(self):
        """The protection that matters most must not have been lost in the
        fix: this image's predecessor shipped live API keys in a layer."""
        patterns = fly_stack.dockerignore_patterns(ROOT)
        for path in (".env", ".env.production", "key.pem", "api.key",
                     "state/trading_state.db"):
            assert fly_stack.excluded_by(patterns, path), path


class TestTheDeployScriptCannotReachMainnet:
    """0046 — `scripts/deploy.sh` is the one command from a checkout to a
    running book. The one thing it must never do is deploy one that can spend
    real money, and that refusal has to be in the script rather than in the
    head of whoever runs it."""

    def script(self):
        path = os.path.join(ROOT, "..", "scripts", "deploy.sh")
        assert os.path.exists(path), "scripts/deploy.sh is missing"
        return open(path, encoding="utf-8").read()

    def test_mainnet_is_refused_by_name(self):
        text = self.script()
        assert "mainnet)" in text
        assert "REFUSED: this script does not deploy to mainnet" in text

    def test_it_deploys_immediate(self):
        assert "--strategy immediate" in self.script()

    def test_it_arms_nothing(self):
        """It may PRINT the arming commands. It may not run them.

        Every occurrence of an arming string must fall after the point where
        the script has stopped executing and started printing instructions.
        """
        body = self.script()
        instructions_begin = body.index("ARMING, when the preflight is clean")
        for armed in ("PAPER_TRADING=0", "LIVE_TRADING_ACK", "drill.py --arm"):
            first = body.find(armed)
            if first == -1:
                continue
            assert first > instructions_begin, (
                f"{armed!r} appears in the executed part of deploy.sh, not "
                "only in the instructions it prints")

    def test_it_checks_the_config_before_creating_anything(self):
        text = self.script()
        assert text.index("fly_stack.py --check") < text.index("fly launch")


class TestTwoMachinesIsTwoBooks:
    def test_the_default_strategy_is_refused(self):
        for strategy in ("rolling", "bluegreen", "canary"):
            cfg = fly_stack.template()
            cfg["deploy"]["strategy"] = strategy
            problems = fly_stack.check(cfg)
            assert any("strategy" in p for p in problems), strategy

    def test_immediate_is_accepted(self):
        assert fly_stack.template()["deploy"]["strategy"] == "immediate"

    def test_a_second_process_group_is_refused(self):
        cfg = fly_stack.template()
        cfg["processes"] = {"bot": "python3 main.py", "worker": "python3 x.py"}
        assert any("process" in p for p in fly_stack.check(cfg))

    def test_a_second_region_is_refused(self):
        """A volume is host-local. Two regions is two volumes is two ledgers
        is two books."""
        cfg = fly_stack.template()
        del cfg["primary_region"]
        assert any("region" in p for p in fly_stack.check(cfg))


class TestTheMachineIsNeverStoppedUnderAnOpenHedge:
    def test_auto_stop_is_refused(self):
        cfg = fly_stack.template()
        cfg.setdefault("http_service", {})["auto_stop_machines"] = "stop"
        assert fly_stack.check(cfg)

    def test_restart_brings_it_back(self):
        """main.py exits 1 when it cannot reach the venue — measured, 0042.
        A machine that stays down after a venue blip is a book that silently
        stops reconciling."""
        assert fly_stack.template()["restart"][0]["policy"] in ("always",
                                                               "on-failure")
        cfg = fly_stack.template()
        cfg["restart"][0]["policy"] = "never"
        assert any("restart" in p for p in fly_stack.check(cfg))


class TestTheLedgerSurvivesADeploy:
    def test_exactly_one_volume_mounted_where_the_db_lives(self):
        cfg = fly_stack.template()
        assert len(cfg["mounts"]) == 1
        assert cfg["mounts"][0]["destination"] == "/app/state"

    def test_the_mount_must_match_the_state_db_path(self):
        cfg = fly_stack.template()
        cfg["env"]["STATE_DB_PATH"] = "/somewhere/else/trading_state.db"
        assert any("STATE_DB_PATH" in p for p in fly_stack.check(cfg))

    def test_no_mount_is_refused(self):
        cfg = fly_stack.template()
        cfg["mounts"] = []
        assert any("volume" in p.lower() for p in fly_stack.check(cfg))


class TestADeployCannotArmLiveTradingOrLeakAKey:
    def test_no_secret_shaped_key_in_env(self):
        for key in ("BYBIT_API_KEY", "BYBIT_API_SECRET", "AUTH_TOKEN",
                    "DB_PASSWORD"):
            cfg = fly_stack.template()
            cfg["env"][key] = "x"
            assert any(key in p for p in fly_stack.check(cfg)), key

    def test_the_shipped_env_arms_nothing(self):
        env = fly_stack.template()["env"]
        assert env["PAPER_TRADING"] == "1"
        assert env["USE_TESTNET"] == "1"
        assert "LIVE_TRADING_ACK" not in env

    def test_arming_live_from_the_repo_is_refused(self):
        for key, value in (("PAPER_TRADING", "0"), ("USE_TESTNET", "0"),
                           ("LIVE_TRADING_ACK", "I_UNDERSTAND")):
            cfg = fly_stack.template()
            cfg["env"][key] = value
            assert any(key in p for p in fly_stack.check(cfg)), key


class TestTheCarryBookGetsWhatBuildBotDemands:
    def test_carry_without_its_required_keys_is_refused(self):
        """build_bot raises on a missing CARRY_BORROW_APR. On Fly that is not
        an error message, it is a crash loop with a restart policy."""
        for key in ("CARRY_BORROW_APR", "CARRY_EXECUTION_MODE"):
            cfg = fly_stack.template()
            del cfg["env"][key]
            assert any(key in p for p in fly_stack.check(cfg)), key

    def test_the_shipped_env_satisfies_config(self):
        """Not a guess about what build_bot wants — config.load is run on it."""
        import config as _config
        env = dict(fly_stack.template()["env"])
        cfg = _config.load(env)
        assert cfg.BOOK_MODE == "carry"
        assert cfg.CARRY_BORROW_APR is not None
        assert cfg.CARRY_EXECUTION_MODE is not None


class TestItIsNotOnThePublicInternet:
    def test_no_public_service_is_declared(self):
        cfg = fly_stack.template()
        assert "services" not in cfg
        cfg["http_service"] = {"internal_port": 8081, "force_https": True}
        assert any("public" in p.lower() for p in fly_stack.check(cfg))

    def test_the_health_check_is_internal_and_matches_the_port(self):
        cfg = fly_stack.template()
        assert cfg["checks"]["health"]["port"] == int(
            cfg["env"]["HEALTHCHECK_PORT"])
        cfg["env"]["HEALTHCHECK_PORT"] = "9999"
        assert any("HEALTHCHECK_PORT" in p for p in fly_stack.check(cfg))

    def test_the_grace_period_covers_startup_reconciliation(self):
        """Reconciliation runs BEFORE the health server binds. A grace period
        shorter than it kills the container mid-cold-start, which is how
        amnesia loops begin."""
        cfg = fly_stack.template()
        assert fly_stack.seconds(cfg["checks"]["health"]["grace_period"]) >= 120
        cfg["checks"]["health"]["grace_period"] = "10s"
        assert any("grace" in p for p in fly_stack.check(cfg))


class TestTheRenderedFileIsTheCheckedOne:
    def test_the_repo_fly_toml_parses_and_passes(self):
        """The committed fly.toml must be what template() produces, or the
        checker is checking a document nobody deploys."""
        import tomllib
        path = os.path.join(ROOT, "fly.toml")
        assert os.path.exists(path), "bot/fly.toml is not committed"
        with open(path, "rb") as handle:
            parsed = tomllib.load(handle)
        assert fly_stack.check(parsed) == []

    def test_the_shipped_template_passes_its_own_checker(self):
        assert fly_stack.check(fly_stack.template()) == []

    def test_rendering_round_trips(self):
        import tomllib
        rendered = fly_stack.to_toml(fly_stack.template())
        assert tomllib.loads(rendered) == fly_stack.template()
