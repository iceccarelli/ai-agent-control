"""A human approval is not a boolean in a file anyone can commit.

The promotion gate used to count a human-owned item complete when the gate JSON
in the repository said `"complete": true`, so an ordinary commit could flip it.
Now such an item counts only if the SHA-256 of its exact entry (key, flag,
evidence, every field) appears in a separate approvals file whose path comes
from configuration and lives outside the repository. Editing the entry after
approval invalidates the approval; a missing or malformed approvals file
approves nothing. Machine-derived items are unchanged, and the live-arming
chain is still consulted separately. Offline.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import promotion_gate as pg  # noqa: E402

NON_MACHINE = [k for k, _d, owner in pg.CHECKLIST_ITEMS if owner != "machine"]


def write_gate(tmp_path, **overrides):
    entries = {k: {"complete": True, "owner": "human", "evidence": f"ev-{k}"}
               for k in NON_MACHINE}
    entries.update(overrides)
    path = tmp_path / "gate.json"
    path.write_text(json.dumps({"schema": "promotion_gate/1",
                                "max_notional_usd": 100.0,
                                "checklist": entries}))
    return str(path), entries


def write_approvals(tmp_path, entries, keys=None):
    approved = {k: pg.approval_digest(k, entries[k])
                for k in (keys if keys is not None else entries)}
    path = tmp_path / "approvals.json"
    path.write_text(json.dumps({"schema": "promotion_approvals/1",
                                "approved": approved}))
    os.chmod(path, 0o600)
    return str(path)


def completed(verdict):
    return {i.key for i in verdict.items if i.complete}


def test_a_flag_alone_approves_nothing(tmp_path):
    gate, _ = write_gate(tmp_path)
    v = pg.evaluate_promotion_gate(path=gate, approvals_path=str(tmp_path / "none"))
    assert completed(v).isdisjoint(NON_MACHINE)
    assert any("approval" in r for r in v.reasons)


def test_the_matching_digest_approves_the_item(tmp_path):
    gate, entries = write_gate(tmp_path)
    ap = write_approvals(tmp_path, entries)
    v = pg.evaluate_promotion_gate(path=gate, approvals_path=ap)
    assert set(NON_MACHINE) <= completed(v)


def test_editing_the_entry_after_approval_invalidates_it(tmp_path):
    gate, entries = write_gate(tmp_path)
    ap = write_approvals(tmp_path, entries)
    changed = dict(entries["human_risk_memo_signed"], evidence="edited later")
    gate2, _ = write_gate(tmp_path, human_risk_memo_signed=changed)
    v = pg.evaluate_promotion_gate(path=gate2, approvals_path=ap)
    assert "human_risk_memo_signed" not in completed(v)
    assert "kill_switch_drill_recorded" in completed(v)      # others unaffected


def test_a_digest_for_another_key_does_not_transfer(tmp_path):
    gate, entries = write_gate(tmp_path)
    ap = write_approvals(tmp_path, entries, keys=[])
    data = json.loads(open(ap).read())
    data["approved"]["human_risk_memo_signed"] = pg.approval_digest(
        "kill_switch_drill_recorded", entries["kill_switch_drill_recorded"])
    open(ap, "w").write(json.dumps(data))
    v = pg.evaluate_promotion_gate(path=gate, approvals_path=ap)
    assert "human_risk_memo_signed" not in completed(v)


@pytest.mark.parametrize("content", ["", "not json", "[]", "null", '{"approved": 3}',
                                     '{"approved": {"x": 5}}',
                                     '{"approved": []}'])
def test_a_malformed_approvals_file_approves_nothing(tmp_path, content):
    gate, _ = write_gate(tmp_path)
    ap = tmp_path / "bad.json"
    ap.write_text(content)
    v = pg.evaluate_promotion_gate(path=gate, approvals_path=str(ap))
    assert completed(v).isdisjoint(NON_MACHINE)


def test_the_digest_ignores_key_order_but_not_content():
    a = {"complete": True, "evidence": "x", "owner": "human"}
    b = {"owner": "human", "evidence": "x", "complete": True}
    assert pg.approval_digest("k", a) == pg.approval_digest("k", b)
    assert pg.approval_digest("k", a) != pg.approval_digest("k", dict(a, evidence="y"))
    assert pg.approval_digest("k", a) != pg.approval_digest("j", a)


def test_an_unapproved_gate_never_allows_live_even_with_every_flag_true(tmp_path):
    gate, _ = write_gate(tmp_path)
    assert pg.promotion_gate_allows_live(
        path=gate, approvals_path=str(tmp_path / "none")) is False


def test_the_repository_gate_file_is_not_self_approving():
    v = pg.evaluate_promotion_gate()
    assert v.allows_live is False
    assert "kill_switch_drill_recorded" not in completed(v)   # no approvals file


def test_the_approve_tool_records_a_digest_and_never_edits_the_gate(tmp_path):
    gate, entries = write_gate(tmp_path)
    before = open(gate).read()
    out = tmp_path / "approvals.json"
    tool = os.path.join(os.path.dirname(HERE), "tools", "promotion_approve.py")
    r = subprocess.run([sys.executable, tool, "--gate", gate, "--key",
                        "kill_switch_drill_recorded", "--out", str(out)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert open(gate).read() == before
    v = pg.evaluate_promotion_gate(path=gate, approvals_path=str(out))
    assert "kill_switch_drill_recorded" in completed(v)
    assert "human_risk_memo_signed" not in completed(v)


def test_the_approve_tool_refuses_an_incomplete_or_unknown_item(tmp_path):
    gate, _ = write_gate(tmp_path, human_risk_memo_signed={
        "complete": False, "owner": "human", "evidence": "no memo"})
    tool = os.path.join(os.path.dirname(HERE), "tools", "promotion_approve.py")
    for key in ("human_risk_memo_signed", "not_a_real_key", "notional_cap_within_policy"):
        out = tmp_path / f"o-{key}.json"
        r = subprocess.run([sys.executable, tool, "--gate", gate, "--key", key,
                            "--out", str(out)], capture_output=True, text=True)
        assert r.returncode != 0, key
        assert not out.exists(), key


# -- where the approvals file may live, and who may write it ------------------

def test_an_approvals_file_inside_the_repository_is_ignored(tmp_path):
    gate, entries = write_gate(tmp_path)
    inside = os.path.join(pg.REPO_ROOT, "bot", "artifacts",
                          "_approvals_inside_repo_test.json")
    approved = {k: pg.approval_digest(k, entries[k]) for k in entries}
    try:
        with open(inside, "w") as handle:
            json.dump({"approved": approved}, handle)
        os.chmod(inside, 0o600)
        v = pg.evaluate_promotion_gate(path=gate, approvals_path=inside)
        assert completed(v).isdisjoint(NON_MACHINE)
        assert any("inside the repository" in r for r in v.reasons)
    finally:
        os.remove(inside)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
@pytest.mark.parametrize("mode", [0o620, 0o602, 0o666, 0o660])
def test_a_group_or_world_writable_approvals_file_is_ignored(tmp_path, mode):
    gate, entries = write_gate(tmp_path)
    ap = write_approvals(tmp_path, entries)
    os.chmod(ap, mode)
    v = pg.evaluate_promotion_gate(path=gate, approvals_path=ap)
    assert completed(v).isdisjoint(NON_MACHINE)
    assert any("writable" in r for r in v.reasons)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_a_symlink_into_the_repository_does_not_launder_the_location(tmp_path):
    gate, entries = write_gate(tmp_path)
    inside = os.path.join(pg.REPO_ROOT, "bot", "artifacts",
                          "_approvals_link_target_test.json")
    link = tmp_path / "link.json"
    approved = {k: pg.approval_digest(k, entries[k]) for k in entries}
    try:
        with open(inside, "w") as handle:
            json.dump({"approved": approved}, handle)
        os.chmod(inside, 0o600)
        os.symlink(inside, link)
        v = pg.evaluate_promotion_gate(path=gate, approvals_path=str(link))
        assert completed(v).isdisjoint(NON_MACHINE)
    finally:
        os.remove(inside)


def test_the_config_path_is_honoured_and_checked_the_same_way(tmp_path):
    gate, entries = write_gate(tmp_path)
    ap = write_approvals(tmp_path, entries)

    class Cfg:
        PROMOTION_APPROVALS_FILE = ap
    v = pg.evaluate_promotion_gate(path=gate, config=Cfg())
    assert set(NON_MACHINE) <= completed(v)
    os.chmod(ap, 0o666)
    v = pg.evaluate_promotion_gate(path=gate, config=Cfg())
    assert completed(v).isdisjoint(NON_MACHINE)


def test_the_approve_tool_refuses_an_output_inside_the_repository(tmp_path):
    gate, _ = write_gate(tmp_path)
    tool = os.path.join(os.path.dirname(HERE), "tools", "promotion_approve.py")
    inside = os.path.join(pg.REPO_ROOT, "bot", "artifacts", "_must_not_exist.json")
    r = subprocess.run([sys.executable, tool, "--gate", gate, "--key",
                        "kill_switch_drill_recorded", "--out", inside],
                       capture_output=True, text=True)
    assert r.returncode == 2 and "inside the repository" in r.stderr
    assert not os.path.exists(inside)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_the_approve_tool_writes_a_private_file(tmp_path):
    gate, _ = write_gate(tmp_path)
    out = tmp_path / "a.json"
    tool = os.path.join(os.path.dirname(HERE), "tools", "promotion_approve.py")
    subprocess.run([sys.executable, tool, "--gate", gate, "--key",
                    "kill_switch_drill_recorded", "--out", str(out)],
                   capture_output=True, text=True, check=True)
    assert (os.stat(out).st_mode & 0o777) == 0o600
