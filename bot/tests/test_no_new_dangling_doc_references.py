"""The set of documents the repository cites but does not contain only shrinks.

About fifty documents (EDGE.md alone is cited ~960 times) are referenced by code,
tests and artifacts and are not in this repository or its history; they came
with the imported source. They cannot be rewritten here: several are claimed to
have been committed before a measurement existed, and a document written now
would fake exactly that. So this test pins the set. A NEW citation of an absent
document fails; a document that is restored must be removed from the list below.
"""
from __future__ import annotations

import os
import re
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SKIP = (".json", ".jsonl", ".db", ".gz", ".csv", ".so", ".png")

KNOWN_ABSENT = frozenset({
    "AGENT_CONTROL_PLANE.md",
    "CORPUS_POLICY.md",
    "DRILL_0045.md",
    "EDGE.md",
    "GEOMETRY.md",
    "HANDOFF.md",
    "HUMAN_DATA_NOTE_SLICE61.md",
    "HUMAN_DATA_NOTE_SLICE62.md",
    "HUMAN_DATA_NOTE_SLICE63.md",
    "HUMAN_DATA_NOTE_SLICE64.md",
    "HUMAN_DATA_NOTE_SLICE65.md",
    "HUMAN_DATA_NOTE_SLICE66.md",
    "INTEGRATION_MAP.md",
    "INVENTORY.md",
    "KILL_SWITCH_DRILL_SCRIPT.md",
    "LEDGER_0044.md",
    "LINEAR_STOP_MARGIN_MEMO.md",
    "LINEAR_STOP_VENUE_GAP.md",
    "LINEAR_STOP_VERIFICATION_CHECKLIST.md",
    "MISSION_SLICE61.md",
    "MISSION_SLICE62.md",
    "MISSION_SLICE63.md",
    "MISSION_SLICE64.md",
    "MISSION_SLICE65.md",
    "MISSION_SLICE66.md",
    "NEXT_MISSION.md",
    "PAPER_AGENT_CERTIFICATION.md",
    "PAPER_OPERATOR_RUNBOOK.md",
    "PAPER_RUNBOOK.md",
    "PHASE1_DECISION.md",
    "PROMOTION_GATE_MICRO_LIVE.md",
    "REPLAY_0043.md",
    "RESEARCH_HOLD.md",
    "RESEARCH_PROGRAM_FREEZE.md",
    "RESEARCH_STATUS.md",
    "RISK_MEMO_MICRO_LIVE_TEMPLATE.md",
    "ROADMAP_FROM_100_DOLLARS.md",
    "STAGE1_VERDICT.md",
    "STAGE1_VERDICT_SLICE55.md",
    "STAGE1_VERDICT_SLICE56.md",
    "STAGE1_VERDICT_SLICE57.md",
    "STAGE1_VERDICT_SLICE58.md",
    "STAGE1_VERDICT_SLICE59.md",
    "STAGE1_VERDICT_SLICE60.md",
    "STAGE1_VERDICT_SLICE61.md",
    "STAGE1_VERDICT_SLICE62.md",
    "STAGE1_VERDICT_SLICE63.md",
    "STAGE1_VERDICT_SLICE64.md",
    "STAGE1_VERDICT_SLICE65.md",
    "STAGE1_VERDICT_SLICE66.md",
    "STAGE1_VERDICT_SLICE73.md",
    "TEST_REPORT.md",
    "VENUE_0046.md",
})


def _scan():
    files = [f for f in subprocess.check_output(
        ["git", "ls-files"], cwd=ROOT, text=True).split("\n") if f]
    exist = {os.path.basename(f) for f in files if f.endswith(".md")}
    cited = set()
    for rel in files:
        if rel.endswith(SKIP):
            continue
        try:
            with open(os.path.join(ROOT, rel), encoding="utf-8") as handle:
                text = handle.read()
        except (OSError, UnicodeDecodeError):
            continue
        cited.update(re.findall(r"\b([A-Z][A-Z0-9_]{2,}\.md)\b", text))
    return exist, cited


def test_no_new_citation_of_an_absent_document():
    exist, cited = _scan()
    new = sorted(c for c in cited if c not in exist and c not in KNOWN_ABSENT)
    assert not new, (
        f"cited but not in the repository: {new}. Add the document, or stop "
        "citing it; do not add it to KNOWN_ABSENT")


def test_a_restored_document_leaves_the_known_absent_list():
    exist, _cited = _scan()
    restored = sorted(KNOWN_ABSENT & exist)
    assert not restored, f"now present, remove from KNOWN_ABSENT: {restored}"
