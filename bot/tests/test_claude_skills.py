"""The project skills under .claude/skills: real, narrow, and safe. The read-only ones can never contain an order-capable or authority-creating command."""
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SK = os.path.join(ROOT, ".claude", "skills")
NAMES = ("money-status", "money-next", "money-research", "money-decision", "money-release")


def read(name):
    return open(os.path.join(SK, name, "SKILL.md"), encoding="utf-8").read()


@pytest.mark.parametrize("name", NAMES)
def test_each_skill_has_valid_frontmatter_and_a_real_workflow(name):
    t = read(name)
    m = re.match(r"^---\nname: ([a-z-]+)\ndescription: (.+)\n---\n", t)
    assert m and m.group(1) == name and len(m.group(2)) > 60, name
    assert t.count("\n") > 8 and f"# /{name}" in t and "Never" in t


def test_the_read_only_skills_never_contain_a_command_that_trades_or_creates_authority():
    for name in ("money-decision", "money-status"):
        code = "\n".join(l for l in read(name).splitlines() if not re.match(r"^\s*(Never|-|\d+\.)|^Never", l))        # the 'Never ...' prohibition lines may NAME the forbidden commands
        for bad in ("autopilot.py loop", "autopilot.py grant", "session.py execute", "unattended_drill.py --conformance", "--arm", "fly deploy", "fly secrets set", "supabase db push", "LIVE_TRADING_ACK"):
            assert bad not in code, (name, bad)
    d = read("money-decision")
    assert "ms_testnet_autopilot.py evaluate" in d and "not authority" in d.lower() and "never run `loop`" in d.lower()


def test_money_release_refuses_when_a_gate_is_missing_and_money_next_uses_the_single_mission_pr():
    assert "NOT READY" in read("money-release") and "EXACT head SHA" in read("money-release") and "3.11 AND 3.12" in read("money-release")
    n = read("money-next")
    assert "never a new PR per task" in n and "docs/MONEY_MISSION.md" in n


def test_skills_do_not_duplicate_claude_md_and_name_no_secret_values():
    for name in NAMES:
        t = read(name)
        assert len(t) < 4500, f"{name} is too long: skills must be narrow"
        assert not re.search(r"(?i)(api[_-]?key|secret)\s*=\s*\S{8,}", t)
