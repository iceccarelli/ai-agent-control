"""The watcher's read-failure classifier, EXECUTED on canned ``flyctl ssh`` stderr (an independent review ran the old shell chain on the same lines and found a dead machine read as a token problem).

Offline and pure. The stderr wordings are illustrative: flyctl's exact messages are not pinned here, the PATTERN CLASSES are (a credential refusal, a machine that is down, a timeout, a refused connection)."""
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path[:0] = [BOT, os.path.join(BOT, "tools")]

import fly_read_failure as FR  # noqa: E402
import runner_alerts as RA  # noqa: E402

CASES = [
    # the lines the review found misclassified by the old chain
    ("Error: app carry-drill has no started VMs", "machine_unreachable"),
    ("machine 148e2403a46698 is stopped", "machine_unreachable"),
    ("Error: dial tcp [fdaa:0:5401:a7b:1234::2]:22: connect: connection refused", "machine_unreachable"),
    ("failed to connect to [fdaa:0:403:a7b::3]:22", "machine_unreachable"),            # an address hextet that looks like an HTTP status
    # a real credential problem, however it is worded
    ("Error: unauthorized", "auth_failed"),
    ("HTTP 401 Unauthorized", "auth_failed"),
    ("Error: status code 403 forbidden", "auth_failed"),
    ("failed to fetch an authentication token: the token is expired", "auth_failed"),
    ("Error: invalid token", "auth_failed"),
    ("an expired token also says: failed to connect to api.fly.io", "auth_failed"),    # the credential class wins over the connection words
    ("permission denied", "auth_failed"),
    # timeouts and refused / absent machines
    ("Error: i/o timeout", "timeout"),
    ("context deadline exceeded", "timeout"),
    ("dial tcp: lookup carry-drill.internal: no such host", "machine_unreachable"),
    ("connect: network is unreachable", "machine_unreachable"),
    ("Error: no machines", "machine_unreachable"),
    # everything else is a failure the watcher could not classify: never silent, never no_data
    ("something nobody has seen before", "unknown"),
    ("", "unknown"),
]


@pytest.mark.parametrize("text,expected", CASES)
def test_canned_stderr_is_classified_by_what_it_says(text, expected):
    assert FR.classify(text) == expected, text


def test_the_classes_are_exactly_the_alert_vocabulary():
    assert set(FR.CLASSES) == set(RA.READ_FAILURES)
    assert RA.READ_FAILURES["machine_unreachable"][1] == RA.CRITICAL and RA.READ_FAILURES["unknown"][1] == RA.CRITICAL


def test_only_the_class_is_ever_printed(tmp_path):
    secret = "fo1_THIS_IS_A_TOKEN_FRAGMENT_0123456789abcdef"
    f = tmp_path / "fly.err"
    f.write_text(f"Error: unauthorized (token {secret} is expired)\n")
    out = subprocess.run([sys.executable, os.path.join(BOT, "tools", "fly_read_failure.py"), str(f)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and out.stdout.strip() == "auth_failed" and secret not in out.stdout + out.stderr


def test_an_unreadable_or_missing_file_is_unknown_not_a_crash(tmp_path):
    out = subprocess.run([sys.executable, os.path.join(BOT, "tools", "fly_read_failure.py"), str(tmp_path / "absent")], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and out.stdout.strip() == "unknown"
    assert FR.main([]) == 2
