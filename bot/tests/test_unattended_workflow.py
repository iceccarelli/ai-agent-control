"""The unattended testnet drill workflow cannot be turned toward mainnet, a pull
request or a fork, and always tries to leave the venue flat.

Text checks on purpose: CI installs no YAML library. The existing gate
(carry-verify.yml) stays secret-free; test_ci_workflow pins that separately.
"""
from __future__ import annotations

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "tools"))

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
WF = os.path.join(ROOT, ".github", "workflows", "carry-drill-testnet.yml")
GATE = os.path.join(ROOT, ".github", "workflows", "carry-verify.yml")


@pytest.fixture(scope="module")
def text():
    return open(WF, encoding="utf-8").read()


def code(text):
    """The workflow with comment lines removed."""
    return "\n".join(l for l in text.split("\n") if not l.lstrip().startswith("#"))


class TestItCannotBeAimedAtTheWrongPlace:
    def test_the_only_triggers_are_schedule_and_manual(self, text):
        on = re.search(r"^on:\n(.*?)^\S", code(text) + "\nx", re.S | re.M).group(1)
        assert "schedule:" in on and "workflow_dispatch:" in on
        for forbidden in ("pull_request", "push:", "pull_request_target",
                          "workflow_run", "issue_comment"):
            assert forbidden not in on, forbidden

    def test_it_is_off_unless_the_repository_variable_says_true(self, text):
        job_if = re.search(r"^    if: (.+)$", code(text), re.M).group(1)
        assert "vars.CARRY_DRILL_ENABLED == 'true'" in job_if

    def test_only_this_repository_and_only_main(self, text):
        job_if = re.search(r"^    if: (.+)$", code(text), re.M).group(1)
        assert "github.repository == 'iceccarelli/ai-agent-control'" in job_if
        assert "github.ref == 'refs/heads/main'" in job_if

    def test_the_venue_is_hard_coded_to_testnet_and_never_live(self, text):
        body = code(text)
        assert 'BYBIT_VENUE: testnet' in body and 'USE_TESTNET: "1"' in body
        assert 'PAPER_TRADING: "0"' in body
        for forbidden in ("LIVE_TRADING_ACK", "mainnet", "api.bybit.com",
                          "BYBIT_VENUE: mainnet", "demo"):
            assert forbidden not in body, forbidden

    def test_permissions_are_read_only(self, text):
        assert re.search(r"^permissions:\n  contents: read$", code(text), re.M)
        assert "write" not in re.search(r"^permissions:\n(  .+\n)+", code(text),
                                        re.M).group(0)

    def test_the_job_has_a_timeout_and_runs_one_at_a_time(self, text):
        body = code(text)
        assert 30 <= int(re.search(r"timeout-minutes: (\d+)", body).group(1)) <= 60
        assert "concurrency:" in body and "cancel-in-progress: false" in body


class TestSecrets:
    def test_testnet_secrets_are_only_ever_step_environment(self, text):
        body = code(text)
        refs = re.findall(r"secrets\.([A-Z0-9_]+)", body)
        assert set(refs) == {"BYBIT_TESTNET_API_KEY", "BYBIT_TESTNET_API_SECRET"}
        # never inside a run: script, an echo or a with: input
        for block in re.findall(r"run: \|\n((?:          .*\n?)+)", body):
            assert "secrets." not in block
        assert "echo" not in "".join(
            l for l in body.split("\n") if "secrets." in l)

    def test_secrets_are_not_job_level_env(self, text):
        job_env = re.search(r"^    env:\n((?:      .+\n)+)", code(text), re.M).group(1)
        assert "secrets." not in job_env and "BYBIT_API" not in job_env

    def test_the_existing_gate_stays_free_of_secrets(self):
        gate = open(GATE, encoding="utf-8").read().lower()
        assert "secrets." not in gate and "carry-drill-testnet" not in gate


class TestItLeavesTheVenueFlat:
    def test_the_safety_net_runs_even_after_failure_or_cancellation(self, text):
        step = re.search(r"- name: Close anything left open.*?(?=\n      - )",
                         code(text), re.S).group(0)
        assert "if: always() && steps.window.outputs.inside == 'true'" in step
        assert "close_testnet_short.py --arm --no-prompt --max-qty 0.0015" in step

    def test_it_comes_after_the_drill_and_before_the_verdict(self, text):
        names = re.findall(r"- name: (.+)", code(text))
        i = {n: k for k, n in enumerate(names)}
        drill = next(k for n, k in i.items() if n.startswith("Armed drill"))
        flat = next(k for n, k in i.items() if n.startswith("Close anything"))
        verdict = i["Verdict"]
        assert drill < flat < verdict

    def test_the_drill_runs_only_after_a_clean_unarmed_preflight(self, text):
        step = re.search(r"- name: Armed drill.*?(?=\n      - )", code(text), re.S).group(0)
        assert "steps.preflight.outcome == 'success'" in step
        assert "steps.window.outputs.inside == 'true'" in step

    def test_the_drill_is_capped_and_crosses_one_settlement(self, text):
        step = re.search(r"- name: Armed drill.*?(?=\n      - )", code(text), re.S).group(0)
        assert "--hold-for-settlement" in step and "--min-settlements 1" in step
        assert "--max-hold-hours 0.5" in step and "--settle-wait-hours 0" in step
        assert "--arm" in step

    def test_a_failed_safety_net_or_preflight_turns_the_run_red(self, text):
        verdict = re.search(r"- name: Verdict.*", code(text), re.S).group(0)
        assert "steps.flat.outcome" in verdict and "exit 1" in verdict
        assert "steps.preflight.outcome" in verdict


class TestTheSchedule:
    def crons(self, text):
        return re.findall(r'- cron: "(\d+) (\d+) \* \* \*"', code(text))

    def test_every_start_is_inside_the_window_if_it_runs_on_time(self, text):
        import next_settlement as N
        crons = self.crons(text)
        assert len(crons) >= 2
        for minute, hour in crons:
            now = (int(hour) * 60 + int(minute)) * 60_000
            inside, lead, _ = N.window_status(now, min_lead_s=240, max_lead_s=1200)
            assert inside, (hour, minute, lead)

    def test_every_settlement_has_two_starts(self, text):
        hours = [int(h) for _m, h in self.crons(text)]
        for settle_hour_before in (23, 7, 15):
            assert hours.count(settle_hour_before) >= 2

    def test_the_window_check_uses_the_same_bounds_as_the_test(self, text):
        assert "--min-lead-minutes 4 --max-lead-minutes 20" in text


class TestThePinnedActionsAndTheArtifact:
    def test_actions_are_the_ones_the_gate_already_uses(self, text):
        used = set(re.findall(r"uses: (actions/[a-z-]+@v\d+)", text))
        assert used == {"actions/checkout@v4", "actions/setup-python@v5",
                        "actions/upload-artifact@v4"}

    def test_the_artifact_is_kept_briefly(self, text):
        assert "retention-days: 14" in text


class TestTheReachabilityProbe:
    def test_it_is_a_plain_public_get_with_no_credentials(self, text):
        step = re.search(r"- name: Can this runner reach the testnet.*?(?=\n      - )",
                         code(text), re.S).group(0)
        assert "https://api-testnet.bybit.com/v5/market/time" in step
        assert "secrets." not in step and "BYBIT_API" not in step
        assert "-X POST" not in step and "--data" not in step
        assert "steps.window" not in step           # it runs on every dispatch

