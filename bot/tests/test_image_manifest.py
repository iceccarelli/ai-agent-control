import hashlib
import os
import re
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(BOT, "tools"))

import image_manifest as IM  # noqa: E402


def machine_output(root, drop=(), alter=(), banner=True):
    """What `sha256sum` prints on a machine: the same lines, optionally with files missing or changed."""
    lines = ["Connecting to carry-drill..."] if banner else []
    for rel in IM.FILES:
        if rel in drop:
            lines.append(f"sha256sum: {rel}: No such file or directory")
            continue
        h = hashlib.sha256(open(os.path.join(root, rel), "rb").read() + (b"x" if rel in alter else b"")).hexdigest()
        lines.append(f"{h}  {rel}")
    return "\n".join(lines) + "\n"


def test_every_compared_file_exists_here_and_is_copied_into_the_drill_image():
    docker = open(os.path.join(BOT, "Dockerfile.drill"), encoding="utf-8").read()
    for rel in IM.FILES:
        assert os.path.isfile(os.path.join(BOT, rel)), rel
        assert re.search(r"(?<![\w/.-])" + re.escape(rel) + r"(?![\w.-])", docker), f"{rel} is not COPYed by Dockerfile.drill"


def test_the_expected_listing_is_exactly_what_sha256sum_would_print():
    text = IM.expected_listing(BOT)
    have, missing = IM.parse(text)
    assert missing == [] and set(have) == set(IM.FILES)
    for rel, h in have.items():
        assert h == hashlib.sha256(open(os.path.join(BOT, rel), "rb").read()).hexdigest()


def test_an_identical_machine_is_identical_and_a_banner_does_not_matter():
    r = IM.compare(IM.expected_listing(BOT), machine_output(BOT))
    assert r["verdict"] == "IDENTICAL_TO_EXPECTED" and r["contains_this_recorder_implementation"] and r["differing"] == []


@pytest.mark.parametrize("rel", IM.CRITICAL)
def test_a_changed_critical_file_means_the_image_does_not_contain_this_recorder(rel):
    r = IM.compare(IM.expected_listing(BOT), machine_output(BOT, alter=(rel,)))
    assert r["verdict"] == "DOES_NOT_CONTAIN_THIS_RECORDER_IMPLEMENTATION" and rel in r["differing"]
    assert r["contains_this_recorder_implementation"] is False


@pytest.mark.parametrize("rel", IM.CRITICAL)
def test_a_missing_critical_file_is_reported_missing_on_machine(rel):
    r = IM.compare(IM.expected_listing(BOT), machine_output(BOT, drop=(rel,)))
    assert {x["file"]: x["status"] for x in r["rows"]}[rel] == "MISSING_ON_MACHINE" and not r["contains_this_recorder_implementation"]


def test_an_image_from_before_the_recorder_existed_lacks_most_of_the_files():
    old = IM.CRITICAL[:2]                                                       # say only these two predate this change
    r = IM.compare(IM.expected_listing(BOT), machine_output(BOT, drop=tuple(f for f in IM.FILES if f not in old), alter=old))
    assert r["verdict"] == "DOES_NOT_CONTAIN_THIS_RECORDER_IMPLEMENTATION"
    assert sum(1 for x in r["rows"] if x["status"] == "MISSING_ON_MACHINE") == len(IM.FILES) - len(old)


def test_differing_supporting_files_alone_are_reported_but_do_not_hide_an_identical_recorder():
    r = IM.compare(IM.expected_listing(BOT), machine_output(BOT, alter=(IM.SUPPORTING[0],)))
    assert r["verdict"] == "RECORDER_FILES_IDENTICAL_SUPPORTING_FILES_DIFFER" and r["contains_this_recorder_implementation"]


def test_a_silent_or_garbled_machine_is_never_read_as_a_match():
    for junk in ("", "Error: app carry-drill has no started VMs\n", "not a listing at all\n"):
        r = IM.compare(IM.expected_listing(BOT), junk)
        assert r["verdict"] == "NOTHING_REPORTED_BY_MACHINE" and not r["contains_this_recorder_implementation"], junk
    short = "\n".join(machine_output(BOT).splitlines()[:3]) + "\n"
    assert not IM.compare(IM.expected_listing(BOT), short)["contains_this_recorder_implementation"]      # files not reported at all


def test_the_printed_command_is_read_only_and_carries_no_secret():
    cmd = IM.machine_command()
    assert cmd.startswith("fly ssh console -a carry-drill -C ") and "sha256sum" in cmd
    assert not re.search(r"(rm |mv |cp |>|tee|secrets|token|key|deploy|scale|restart|stop|destroy|volumes)", cmd.replace("2>&1", ""), re.I)
    assert all(f in cmd for f in IM.FILES)


def test_the_cli_exit_codes(tmp_path, capsys):
    exp = tmp_path / "expected.txt"
    exp.write_text(IM.expected_listing(BOT))
    act = tmp_path / "actual.txt"
    act.write_text(machine_output(BOT))
    assert IM.main(["compare", "--expected", str(exp), "--actual", str(act)]) == 0
    act.write_text(machine_output(BOT, alter=(IM.CRITICAL[0],)))
    assert IM.main(["compare", "--expected", str(exp), "--actual", str(act)]) == 1
    assert IM.main(["command"]) == 0 and "fly ssh console" in capsys.readouterr().out
