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



# ---------------------------------------------------------------------------
# found by independent review
# ---------------------------------------------------------------------------

import subprocess  # noqa: E402
import sys as _sys  # noqa: E402


def step_script(text, name_start):
    """The `run:` script of the step whose name starts with `name_start`."""
    step = re.search(r"- name: " + re.escape(name_start) + r".*?(?=\n      - |\Z)",
                     code(text), re.S).group(0)
    block = re.search(r"run: \|\n((?:          .*\n?)+)", step).group(1)
    return "\n".join(l[10:] for l in block.split("\n"))


class TestEnvironmentAndGuards:
    def test_the_credentials_live_in_a_branch_restricted_environment(self, text):
        assert re.search(r"^    environment: testnet-drill$", code(text), re.M)

    def test_a_public_repository_is_refused_unless_explicitly_allowed(self, text):
        job_if = re.search(r"^    if: (.+)$", code(text), re.M).group(1)
        assert "github.event.repository.private == true" in job_if
        assert "vars.CARRY_DRILL_ALLOW_PUBLIC == 'true'" in job_if

    def test_dependencies_are_installed_against_the_tested_pins(self, text):
        assert "pip install -r requirements.txt -c constraints.txt" in text

    def test_the_armed_step_has_its_own_timeout_below_the_jobs(self, text):
        step = re.search(r"- name: Armed drill.*?(?=\n      - )", code(text), re.S).group(0)
        step_t = int(re.search(r"timeout-minutes: (\d+)", step).group(1))
        job_t = int(re.search(r"^    timeout-minutes: (\d+)", code(text), re.M).group(1))
        assert 30 <= step_t <= job_t - 10

    def test_a_rerun_gets_its_own_artifact_name(self, text):
        assert "${{ github.run_id }}-${{ github.run_attempt }}" in text


class TestTheWindowStepAsShell:
    def run(self, tmp_path, exit_code):
        (tmp_path / "tools").mkdir()
        (tmp_path / "tools" / "next_settlement.py").write_text(
            f"import sys\nprint('seconds_to_settlement=600')\nsys.exit({exit_code})\n")
        bindir = tmp_path / "bin"
        bindir.mkdir()
        (bindir / "python").symlink_to(_sys.executable)
        env = {"PATH": f"{bindir}:/usr/bin:/bin", "GITHUB_OUTPUT": str(tmp_path / "out"),
               "GITHUB_ENV": str(tmp_path / "env"), "GITHUB_STEP_SUMMARY": str(tmp_path / "sum"),
               "RUNNER_TEMP": str(tmp_path / "tmp")}
        script = step_script(open(WF, encoding="utf-8").read(), "Is a settlement")
        done = subprocess.run(["bash", "-e", "-c", script], cwd=tmp_path, env=env,
                              capture_output=True, text=True)
        out = (tmp_path / "out").read_text() if (tmp_path / "out").exists() else ""
        return done.returncode, out

    def test_inside_is_true(self, tmp_path):
        rc, out = self.run(tmp_path, 0)
        assert rc == 0 and "inside=true" in out

    def test_exit_10_is_outside_and_not_an_error(self, tmp_path):
        rc, out = self.run(tmp_path, 10)
        assert rc == 0 and "inside=false" in out

    @pytest.mark.parametrize("code_", [1, 3, 127])
    def test_any_other_exit_is_a_broken_step_not_a_quiet_green(self, tmp_path, code_):
        rc, out = self.run(tmp_path, code_)
        assert rc == 1 and "inside=false" in out


class TestTheVerdictAsShell:
    def verdict(self, drill, preflight, flat, recon):
        script = step_script(open(WF, encoding="utf-8").read(), "Verdict")
        script = (script.replace("${{ steps.drill.outputs.exit }}", drill)
                  .replace("${{ steps.preflight.outcome }}", preflight)
                  .replace("${{ steps.flat.outcome }}", flat)
                  .replace("${{ steps.reconcile.outputs.recon }}", recon))
        assert "${{" not in script
        return subprocess.run(["bash", "-e", "-c", script], capture_output=True,
                              text=True).returncode

    def test_a_clean_run_is_green(self):
        assert self.verdict("5", "success", "success", "2") == 0
        assert self.verdict("0", "success", "success", "0") == 0

    @pytest.mark.parametrize("args", [
        ("5", "success", "failure", "2"),      # could not verify flat
        ("5", "failure", "success", "2"),      # preflight failed
        ("130", "success", "success", "2"),    # interrupted
        ("", "success", "success", "2"),       # cancelled / timed out
        ("4", "success", "success", "2"),
        ("5", "success", "success", "3"),      # reconcile crashed
        ("5", "success", "success", "99"),     # no usable report
        ("5", "success", "success", ""),       # reconcile never ran
    ])
    def test_everything_else_is_red(self, args):
        assert self.verdict(*args) == 1


class TestTheReconcileStepReportsItsExit:
    def test_it_writes_recon_to_the_outputs_and_flags_a_missing_report(self, text):
        script = step_script(text, "Reconcile the capture")
        assert 'echo "recon=$rc" >> "$GITHUB_OUTPUT"' in script
        assert "rc=99" in script


class TestTheProbeFailsClosed:
    def test_a_non_200_stops_the_job_instead_of_carrying_on(self, text):
        script = step_script(text, "Can this runner reach the testnet")
        assert '[ "$code" != "200" ]' in script and "exit 1" in script
        assert "NOT reachable" in script

