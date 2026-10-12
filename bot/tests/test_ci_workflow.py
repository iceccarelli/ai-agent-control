"""The CI gate stays real: it names tests that exist, hides nothing, needs no secret."""
from __future__ import annotations

import os
import re

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
WORKFLOW = os.path.join(os.path.dirname(BOT), ".github", "workflows",
                        "carry-verify.yml")

REQUIRED = (
    "test_carry_funding_truth", "test_carry_fee_truth", "test_carry_report",
    "test_carry_broker", "test_carry_preflight", "test_fee_payload_probe",
    "test_carry_live_gates")


@pytest.fixture(scope="module")
def text():
    with open(WORKFLOW, encoding="utf-8") as fh:
        return fh.read()


def test_it_runs_on_pull_requests_and_pushes_to_main(text):
    # Text check on purpose: CI installs only requirements.txt + pytest, so this
    # test must not need a YAML library.
    assert re.search(r"^on:\n  pull_request:\n  push:\n    branches: \[main\]\n",
                     text, re.M), "triggers must be pull_request and push to main"


def test_every_test_file_it_runs_exists_and_the_required_ones_are_present(text):
    named = re.findall(r"tests/(test_[A-Za-z0-9_]+\.py)", text)
    assert named, "the workflow names no tests"
    for name in named:
        assert os.path.exists(os.path.join(HERE, name)), name
    for needed in REQUIRED:
        assert f"{needed}.py" in named, needed


def test_it_runs_the_fly_check_and_a_checkout_safe_diff_check(text):
    assert "python tools/fly_stack.py --check" in text
    assert "git diff --check HEAD^1 HEAD" in text         # the change, not the tree
    assert "fetch-depth: 2" in text


def test_it_uses_the_supported_pythons(text):
    assert '"3.11"' in text and '"3.12"' in text
    with open(os.path.join(BOT, "Dockerfile"), encoding="utf-8") as fh:
        assert "python:3.11" in fh.read()


def test_it_needs_no_secret_and_cannot_reach_a_venue_or_trade(text):
    lowered = text.lower()
    assert "secrets." not in lowered
    for forbidden in ("bybit_api_key", "bybit_api_secret", "live_trading_ack",
                      "testnet_api", "api-testnet", "api.bybit", "paper_trading"):
        assert forbidden not in lowered, forbidden
    assert re.search(r"^permissions:\n  contents: read\n", text, re.M), \
        "the workflow must grant read-only repository access and nothing else"
    assert len(re.findall(r"^\s*(?:id-token|pull-requests|issues|actions|"
                          r"packages|deployments):\s*write", text, re.M)) == 0


def test_it_hides_no_failure(text):
    assert "continue-on-error" not in text
    assert "|| true" not in text
    assert "--ignore" not in text and "-k " not in text and "deselect" not in text
    assert "xfail" not in text.lower()


def test_it_does_not_pretend_to_be_the_full_suite(text):
    assert "pytest -q -p no:cacheprovider tests\n" not in text
    assert "69 failures" in text and "NOT run or hidden" in text
