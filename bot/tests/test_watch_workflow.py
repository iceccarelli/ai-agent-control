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


def _steps(code):
    """The workflow's steps as (name-or-uses, block-text), split on '- ' items."""
    body = code.split("steps:", 1)[1]
    return [b for b in re.split(r"\n      - ", "\n" + body) if b.strip()]


def test_no_secret_is_visible_to_a_third_party_action():
    # a job-level env hands every secret to every step's action, including the
    # third-party setup-flyctl (pinned, still untrusted): each secret must sit only on the step that uses it
    job_head = CODE.split("steps:", 1)[0]
    assert "secrets." not in job_head, "a secret is exposed at job level"
    steps = _steps(CODE)
    holders = {s: [m for m in re.findall(r"secrets\.([A-Z_]+)", s)] for s in steps}
    for block, names in holders.items():
        if "uses:" in block.split("\n", 1)[0] or block.lstrip().startswith("uses:"):
            assert not names, f"an action step receives a secret: {block[:60]!r}"
    assert sorted(n for names in holders.values() for n in names) == [
        "FLY_API_TOKEN", "SLACK_WEBHOOK_URL"]


def test_each_secret_is_on_exactly_the_step_that_needs_it():
    steps = _steps(CODE)
    fly = [s for s in steps if "secrets.FLY_API_TOKEN" in s]
    slack = [s for s in steps if "secrets.SLACK_WEBHOOK_URL" in s]
    assert len(fly) == 1 and "flyctl ssh console" in fly[0]
    assert len(slack) == 1 and "runner_alerts.py" in slack[0]


def test_an_unreadable_history_is_retried_once_and_its_cause_is_shown():
    # one transient `fly ssh` failure used to page "no_data" with no reason at
    # all (stderr went to a file nobody printed)
    step = [s for s in _steps(CODE) if "flyctl ssh console" in s]
    assert len(step) == 1
    s = step[0]
    assert "for attempt in 1 2" in s
    assert re.search(r"tail -n \d+ fly\.err", s), "the failure cause is never printed"
    assert s.count("flyctl ssh console") == 1       # still one read, in a loop
    # persistent failure is still not hidden: the history ends up empty and the
    # alert rules run on it
    assert ": > history.jsonl" in s


def test_the_retry_does_not_become_a_mutating_or_hiding_command():
    assert "|| true" not in CODE and "continue-on-error" not in CODE
    assert [m for m in re.findall(r'-C "([^"]*)"', CODE)] == ["cat /data/monitor/monitor.jsonl"]


def test_a_failed_read_is_classified_into_the_closed_vocabulary_and_never_forwarded_as_text():
    """An expired token and an unreachable machine used to both page `no_data`. The read step now writes ONLY a class word; the alert step passes it to runner_alerts."""
    steps = _steps(CODE)
    read = [s for s in steps if "flyctl ssh console" in s][0]
    alert = [s for s in steps if "runner_alerts.py" in s][0]
    assert "python bot/tools/fly_read_failure.py fly.err > read_failure.txt" in read, "the classification is code a test executes (tests/test_fly_read_failure.py), not a grep chain in the workflow"
    assert "grep -qiE" not in read and "echo auth_failed" not in read, "the old grep chain (bare `token`, `401|403`) must not come back"
    assert "--read-failure" in alert and 'cat read_failure.txt' in alert
    assert 'cat fly.err' not in alert and "fly.err" not in alert, "error text must never reach the alert step (it can carry a token fragment)"
    import runner_alerts as RA
    for word in ("auth_failed", "timeout", "machine_unreachable", "unknown"):
        assert word in RA.READ_FAILURES, word


def test_every_workflow_that_reads_a_secret_declares_a_job_level_environment():
    """Round 19 (adversarial review): a repository-level secret is delivered to a workflow run from ANY branch (a workflow_dispatch from a pushed branch whose copy of the file drops its `if:`); an
    environment secret with a main-only deployment-branch policy is not. Every job that reads a secret other than GITHUB_TOKEN names an environment (the owner then creates it, moves the
    secrets and deletes the repository-level copies; naming it changes nothing until then)."""
    wf_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".github", "workflows")
    bad = []
    for name in sorted(os.listdir(wf_dir)):
        code = "\n".join(l for l in open(os.path.join(wf_dir, name), encoding="utf-8").read().splitlines() if not l.lstrip().startswith("#"))
        if re.search(r"secrets\.(?!GITHUB_TOKEN)[A-Z_]+", code) and not re.search(r"^    environment: \S+$", code, re.M):
            bad.append(name)
    assert not bad, bad


# ------------------------------------------------------------------------------------------------ Round 20: the enable variable is a REPOSITORY variable (a job-level `if` cannot see an environment-scoped one)

import os as _os  # noqa: E402

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))


def test_each_read_only_workflow_says_its_enable_variable_must_stay_a_repository_variable():
    """REPRODUCED (Round 20, specialist A): `fly-heartbeat-monitor` runs as `skipped`. The job-level `if` reads `vars.<ENABLE>` before the job's environment is bound, so an environment-scoped variable
    is empty there and the job is skipped silently. The Round 19 owner step said only 'move the secrets'; an owner who also moved the variable would have lost the alarm that found the recorder incident."""
    for rel, var in ((".github/workflows/fly-heartbeat-monitor.yml", "FLY_MONITOR_ENABLED"), (".github/workflows/carry-watch.yml", "CARRY_WATCH_ENABLED")):
        text = open(_os.path.join(_ROOT, rel), encoding="utf-8").read()
        assert f"vars.{var} == 'true'" in text
        assert "REPOSITORY variable" in text and "silently `skipped`" in text and "A `skipped` run is not a health check" in text, rel
    handover = open(_os.path.join(_ROOT, "docs", "MONEY_MISSION.md"), encoding="utf-8").read()
    assert "must stay REPOSITORY variables" in handover and "FLY_MONITOR_ENABLED" in handover and "CARRY_WATCH_ENABLED" in handover
