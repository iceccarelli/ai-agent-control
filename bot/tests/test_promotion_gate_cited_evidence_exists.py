"""Lie detector for the committed promotion gate.

No checklist item in artifacts/slice59_promotion_gate.json may be
`complete: true` while citing a `*.json` transcript that does not exist on
disk. The linear-stop item once did exactly that (eight cited transcripts, none
ever committed), so a reader of the gate saw "verified" over nothing.

This only makes the gate stricter. Offline; reads committed files only.
"""
from __future__ import annotations

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
ARTIFACTS = os.path.join(BOT, "artifacts")
GATE = os.path.join(ARTIFACTS, "slice59_promotion_gate.json")
sys.path.insert(0, BOT)

import promotion_gate as pg  # noqa: E402

_JSON_NAME = re.compile(r"[A-Za-z0-9_./\-]+\.json")


def _strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from _strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _strings(v)


def cited_missing(gate, artifacts_dir=ARTIFACTS):
    """{item: [cited json basenames absent from artifacts_dir]} for every item
    that claims complete=true."""
    out = {}
    for key, entry in gate["checklist"].items():
        if not isinstance(entry, dict) or entry.get("complete") is not True:
            continue
        names = set()
        for s in _strings(entry):
            for tok in _JSON_NAME.findall(s):
                names.add(os.path.basename(tok))
        missing = sorted(n for n in names
                         if not os.path.isfile(os.path.join(artifacts_dir, n)))
        if missing:
            out[key] = missing
    return out


def _gate():
    with open(GATE, encoding="utf-8") as fh:
        return json.load(fh)


def test_no_complete_item_cites_a_transcript_that_does_not_exist():
    assert cited_missing(_gate()) == {}


def test_detector_catches_a_fabricated_citation(tmp_path):
    gate = {"checklist": {
        "x": {"complete": True, "evidence": "see bot/artifacts/ghost.json"},
        "y": {"complete": False, "evidence": "see ghost2.json"}}}
    assert cited_missing(gate, str(tmp_path)) == {"x": ["ghost.json"]}


def test_linear_stop_item_is_incomplete_until_transcripts_are_committed():
    # Deliberately changed from complete=true / items_complete=4: the eight
    # cited transcripts do not exist in this repository or its history.
    gate = _gate()
    item = gate["checklist"]["linear_protective_stop_verified"]
    assert item["complete"] is False
    assert "Vincenzo Ceccarelli" in item["evidence"]       # prior text preserved
    assert "linear_stop_venue_drill.py" in item["how_to_satisfy"]
    done = sum(1 for e in gate["checklist"].values() if e.get("complete") is True)
    assert gate["items_complete"] == done == 3


def test_the_gate_still_never_allows_live():
    verdict = pg.evaluate_promotion_gate()
    assert verdict.allows_live is False
    assert pg.promotion_gate_allows_live() is False
