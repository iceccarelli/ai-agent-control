"""The external watcher stays read-only, off by default and secret-minimal."""
from __future__ import annotations

import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TEXT = open(os.path.join(ROOT, ".github", "workflows", "carry-watch.yml"), encoding="utf-8").read()
CODE = "\n".join(l for l in TEXT.splitlines() if not l.lstrip().startswith("#"))


def test_it_is_off_unless_the_variable_is_true_and_only_runs_from_main_here():
    assert "vars.CARRY_WATCH_ENABLED == 'true'" in CODE
    assert "github.ref == 'refs/heads/main'" in CODE
    assert "github.repository == 'iceccarelli/ai-agent-control'" in CODE


def test_it_reads_contents_only_and_uses_exactly_two_secrets():
    assert re.search(r"permissions:\s*\n\s*contents: read", CODE)
    assert sorted(set(re.findall(r"secrets\.([A-Z_]+)", CODE))) == ["FLY_API_TOKEN", "SLACK_WEBHOOK_URL"]


def test_it_has_no_venue_key_trading_ack_or_mutating_fly_command():
    low = CODE.lower()
    for bad in ("bybit_api", "live_trading_ack", "fly deploy", "flyctl deploy", "machine stop",
                "machine restart", "machine destroy", "secrets set", "volumes", "orders", "pull_request"):
        assert bad not in low, bad


def test_the_only_remote_command_is_a_read_of_the_history_file():
    cmds = re.findall(r'-C "([^"]*)"', CODE)
    assert cmds == ["cat /data/monitor/monitor.jsonl"]


def test_a_firing_alert_fails_the_job_and_nothing_hides_it():
    assert "runner_alerts.py history.jsonl" in CODE
    assert "continue-on-error" not in CODE and "|| true" not in CODE


def test_it_is_in_the_ci_lists():
    wf = open(os.path.join(ROOT, ".github", "workflows", "carry-verify.yml"), encoding="utf-8").read()
    assert "tests/test_watch_workflow.py" in wf
