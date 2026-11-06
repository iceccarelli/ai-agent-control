import hashlib
import os
import re
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(BOT, "tools"))

import image_manifest as IM  # noqa: E402

REC, TOOLS = IM.file_sets(BOT)


def machine_output(drop=(), alter=(), banner=True):
    """What `sha256sum` prints on a machine: the same lines, optionally with files missing or changed."""
    lines = ["Connecting to carry-drill..."] if banner else []
    for rel in REC + TOOLS:
        if rel in drop:
            lines.append(f"sha256sum: {rel}: No such file or directory")
            continue
        h = hashlib.sha256(open(os.path.join(BOT, rel), "rb").read() + (b"x" if rel in alter else b"")).hexdigest()
        lines.append(f"{h}  {rel}")
    return "\n".join(lines) + "\n"


def test_the_recorder_files_are_the_whole_static_import_closure_of_its_entry_points():
    assert set(IM.ENTRY_POINTS) <= set(REC)
    # the modules the review named as missing from the old hand-picked list are now compared
    for must in ("md_book.py", "md_wire.py", "md_rest.py", "md_transport.py", "md_events.py", "ws_transport.py", "venues.py",
                 "md_state.py", "md_store.py", "md_engine.py", "md_feed.py", "md_capacity.py", "evidence_chain.py"):
        assert must in REC, must
    assert len(REC) >= 25 and not set(REC) & set(TOOLS)


def test_every_compared_file_exists_here_and_is_copied_into_the_drill_image():
    docker = open(os.path.join(BOT, "Dockerfile.drill"), encoding="utf-8").read()
    for rel in REC + TOOLS:
        assert os.path.isfile(os.path.join(BOT, rel)), rel
        top = rel.split("/")[0] + "/"
        whole_dir = "/" in rel and not rel.startswith("tools/") and re.search(r"^COPY\s+" + re.escape(top) + r"\s", docker, re.M)
        assert whole_dir or re.search(r"(?<![\w/.-])" + re.escape(rel) + r"(?![\w.-])", docker), f"{rel} is not COPYed by Dockerfile.drill"


def test_the_expected_listing_is_exactly_what_sha256sum_would_print_with_a_marker_between_the_two_parts():
    text = IM.expected_listing(BOT)
    have, missing = IM.parse(text)
    assert missing == [] and set(have) == set(REC + TOOLS) and IM.MARKER in text
    assert IM._split_expected(text) == (REC, TOOLS)
    for rel, h in have.items():
        assert h == hashlib.sha256(open(os.path.join(BOT, rel), "rb").read()).hexdigest()


def test_an_identical_machine_is_identical_and_a_banner_does_not_matter():
    r = IM.compare(IM.expected_listing(BOT), machine_output())
    assert r["verdict"] == "IDENTICAL_TO_EXPECTED" and r["contains_this_recorder_implementation"] and r["differing"] == []
    assert r["recorder_files_compared"] == len(REC)


@pytest.mark.parametrize("rel", ["tools/market_recorder.py", "md_book.py", "md_wire.py", "evidence_chain.py", "md_feed.py", "venues.py"])
def test_a_changed_file_anywhere_in_the_recorders_closure_means_the_image_does_not_contain_this_recorder(rel):
    r = IM.compare(IM.expected_listing(BOT), machine_output(alter=(rel,)))
    assert r["verdict"] == "DOES_NOT_CONTAIN_THIS_RECORDER_IMPLEMENTATION" and rel in r["differing"]
    assert r["contains_this_recorder_implementation"] is False


@pytest.mark.parametrize("rel", ["tools/runner_monitor.py", "md_store.py", "ws_transport.py"])
def test_a_missing_file_in_the_closure_is_reported_missing_on_machine(rel):
    r = IM.compare(IM.expected_listing(BOT), machine_output(drop=(rel,)))
    assert {x["file"]: x["status"] for x in r["rows"]}[rel] == "MISSING_ON_MACHINE" and not r["contains_this_recorder_implementation"]


def test_an_image_from_before_the_recorder_existed_lacks_most_of_the_files():
    old = REC[:2]
    r = IM.compare(IM.expected_listing(BOT), machine_output(drop=tuple(f for f in REC + TOOLS if f not in old), alter=old))
    assert r["verdict"] == "DOES_NOT_CONTAIN_THIS_RECORDER_IMPLEMENTATION"
    assert sum(1 for x in r["rows"] if x["status"] == "MISSING_ON_MACHINE") == len(REC + TOOLS) - len(old)


def test_differing_report_tools_alone_do_not_hide_an_identical_recorder():
    r = IM.compare(IM.expected_listing(BOT), machine_output(alter=(TOOLS[0],)))
    assert r["verdict"] == "RECORDER_FILES_IDENTICAL_REPORT_TOOLS_DIFFER" and r["contains_this_recorder_implementation"]


def test_a_silent_or_garbled_machine_is_never_read_as_a_match():
    for junk in ("", "Error: app carry-drill has no started VMs\n", "not a listing at all\n"):
        r = IM.compare(IM.expected_listing(BOT), junk)
        assert r["verdict"] == "NOTHING_REPORTED_BY_MACHINE" and not r["contains_this_recorder_implementation"], junk
    short = "\n".join(machine_output().splitlines()[:3]) + "\n"
    assert not IM.compare(IM.expected_listing(BOT), short)["contains_this_recorder_implementation"]


def test_an_expected_listing_with_no_recorder_files_cannot_vacuously_match():
    r = IM.compare("", machine_output())
    assert r["contains_this_recorder_implementation"] is False and r["recorder_files_compared"] == 0


def test_the_printed_command_is_read_only_and_carries_no_secret():
    cmd = IM.machine_command(root=BOT)
    assert cmd.startswith("fly ssh console -a carry-drill -C ") and "sha256sum" in cmd
    assert not re.search(r"(rm |mv |cp |>|tee|secrets|token|key|deploy|scale|restart|stop|destroy|volumes)", cmd.replace("2>&1", ""), re.I)
    assert all(f in cmd for f in REC + TOOLS)


def test_the_cli_exit_codes(tmp_path, capsys):
    exp = tmp_path / "expected.txt"
    exp.write_text(IM.expected_listing(BOT))
    act = tmp_path / "actual.txt"
    act.write_text(machine_output())
    assert IM.main(["compare", "--expected", str(exp), "--actual", str(act)]) == 0
    act.write_text(machine_output(alter=("md_book.py",)))
    assert IM.main(["compare", "--expected", str(exp), "--actual", str(act)]) == 1
    assert IM.main(["command"]) == 0 and "fly ssh console" in capsys.readouterr().out


def test_a_package_the_recorder_imports_is_compared_file_by_file_not_missed():
    """`from signals import x` is a file of the package (and its __init__), not an unresolvable name (delta review L9)."""
    assert "signals/__init__.py" in REC and "signals/funding_carry_fade_btc_v1.py" in REC
    imports = IM._imports(os.path.join(BOT, "shadow.py"))
    assert "signals" in imports and "signals.funding_carry_fade_btc_v1" in imports
    assert IM._candidates("signals.funding_carry_fade_btc_v1")[0] == "signals/funding_carry_fade_btc_v1.py"


def test_the_closure_counts_lazy_imports_and_says_so():
    assert "LAZY imports count" in IM.closure.__doc__
    assert "carry_engine.py" in REC           # reached through runner_monitor's function-level import of carry_shadow: a file the process may load
