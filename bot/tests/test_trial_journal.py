"""tools/trial_journal.py: looks at the frozen prospective evaluation survive a restart and cannot be quietly shortened. Synthetic data only."""
from __future__ import annotations

import ast
import json
import os
import sys

import pytest

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BOT, "tools"))
sys.path.insert(0, BOT)

import carry_prospective as CP  # noqa: E402
import statistical_power as SP  # noqa: E402
import trial_journal as TJ  # noqa: E402
from test_carry_prospective import CLOSED, window_prints  # noqa: E402


@pytest.fixture(autouse=True)
def _clock(monkeypatch):
    monkeypatch.setattr(CP, "_now_ms", lambda: CLOSED)


def _look(path, seed, anchor=None, hurdle=10.0):
    return TJ.evaluate_logged(window_prints(0.3, seed=seed), hurdle, str(path), anchor=anchor)


def test_looks_are_counted_across_restarts(tmp_path):
    j = tmp_path / "looks.jsonl"
    counts = [_look(j, s)["journal"]["n_trials"] for s in (1, 2, 3)]      # three separate "processes", each loading from disk
    assert counts == [1, 2, 3]
    assert TJ.load(str(j)).n_trials == 3


def test_the_correction_sees_every_earlier_look(tmp_path):
    j = tmp_path / "looks.jsonl"
    first = _look(j, 1)
    later = _look(j, 2)
    assert later["journal"]["n_trials"] == first["journal"]["n_trials"] + 1


def test_a_shortened_journal_is_refused_against_its_anchor_and_reports_no_anchor_without_one(tmp_path):
    j = tmp_path / "looks.jsonl"
    _look(j, 1)
    anchor = SP.LedgerAnchor(*(lambda a: (a["n"], a["head"]))(_look(j, 2)["journal"]["anchor_to_keep_outside_the_journal"]))
    lines = j.read_text().splitlines(keepends=True)
    j.write_text("".join(lines[:-1]))                                      # remove the last look
    with pytest.raises(SP.LedgerTamperError, match="shortened"):
        TJ.load(str(j), anchor)
    unanchored = TJ.evaluate_logged(window_prints(0.3, seed=9), 10.0, str(j))
    assert unanchored["journal"]["limit"] and unanchored["journal"]["checked_against_an_anchor"] is False


def test_a_vanished_journal_is_refused_against_an_anchor(tmp_path):
    j = tmp_path / "looks.jsonl"
    anchor = SP.LedgerAnchor(*(lambda a: (a["n"], a["head"]))(_look(j, 1)["journal"]["anchor_to_keep_outside_the_journal"]))
    j.unlink()
    with pytest.raises(SP.LedgerTamperError):
        TJ.load(str(j), anchor)


@pytest.mark.parametrize("edit", [
    lambda t: t.replace('"statistic": ', '"statistic": 9', 1),
    lambda t: t.replace('"pvalue": ', '"pvalue": 0.', 1),
    lambda t: t.replace('"index": 1', '"index": 0', 1),
    lambda t: t.rstrip("\n"),                                              # partial last line
    lambda t: t + "not json\n",
    lambda t: t.splitlines(keepends=True)[1] + t.splitlines(keepends=True)[0],     # reordered
])
def test_an_edited_reordered_or_partial_journal_is_refused(tmp_path, edit):
    j = tmp_path / "looks.jsonl"
    _look(j, 1)
    _look(j, 2)
    j.write_text(edit(j.read_text()))
    with pytest.raises(SP.LedgerTamperError):
        TJ.load(str(j))


def test_a_refused_evaluation_appends_nothing(tmp_path, monkeypatch):
    j = tmp_path / "looks.jsonl"
    _look(j, 1)
    before = j.read_text()
    monkeypatch.setattr(CP, "_now_ms", lambda: CP.WINDOW_START_MS)
    with pytest.raises(SP.WindowCoverageError):
        _look(j, 2)
    assert j.read_text() == before


def test_a_link_is_not_a_journal(tmp_path):
    real = tmp_path / "real.jsonl"
    _look(real, 1)
    link = tmp_path / "link.jsonl"
    os.symlink(real, link)
    with pytest.raises(SP.LedgerTamperError, match="regular file"):
        TJ.load(str(link))


def test_the_module_touches_no_network_process_or_database():
    tree = ast.parse(open(TJ.__file__, encoding="utf-8").read())
    imported = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imported |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            imported.add(n.module.split(".")[0])
    assert imported <= {"__future__", "json", "os", "sys", "typing", "carry_prospective", "statistical_power"}, imported


def test_an_edit_to_the_last_look_alone_is_caught_by_its_stored_hash(tmp_path):
    # Nothing follows the last line, so no later prev_hash can expose an edit to it: only its own stored hash does.
    j = tmp_path / "looks.jsonl"
    _look(j, 1)
    lines = j.read_text().splitlines(keepends=True)
    assert len(lines) == 1
    row = json.loads(lines[0])
    row["statistic"] = row["statistic"] + 1.0
    j.write_text(json.dumps(row, sort_keys=True) + "\n")
    with pytest.raises(SP.LedgerTamperError, match="does not match"):
        TJ.load(str(j))
