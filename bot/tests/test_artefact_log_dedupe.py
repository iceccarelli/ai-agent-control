"""The artefact reader's refusals are logged once, not on every status build.

`cleared_edge_signal_from_artifacts` is called repeatedly; each call re-read the
same artefacts and re-logged the same refusals. The DECISION must be unchanged
(still None, still fail closed); only the repeats are quieter.
"""
from __future__ import annotations

import json
import logging
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import project_status as ps  # noqa: E402


def write(dirpath, name, **over):
    summary = {"verdict": "EDGE_EVIDENCE_POSITIVE", "signal": "some_new_signal",
               "m1": {"percentile": 99.0}, "m2": {"percentile": 99.0, "passed": True}}
    summary.update(over)
    path = dirpath / name
    path.write_text(json.dumps(summary))
    return path


def refusals(caplog, level=logging.WARNING):
    return [r for r in caplog.records if r.levelno >= level and "artefact" in r.getMessage()]


def call(d, caplog, times=3):
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="project_status"):
        out = [ps.cleared_edge_signal_from_artifacts(str(d)) for _ in range(times)]
    return out


class TestRefusalsAreLoggedOnce:
    def test_an_unattested_artefact_warns_once_and_is_refused_every_time(
            self, tmp_path, caplog):
        write(tmp_path, "a_summary.json")                 # no control_validated
        out = call(tmp_path, caplog)
        assert out == [None, None, None]
        warned = [r for r in refusals(caplog) if r.levelno == logging.WARNING]
        assert len(warned) == 1 and "refusing" in warned[0].getMessage()
        debug = [r for r in caplog.records if r.levelno == logging.DEBUG
                 and "refusing" in r.getMessage()]
        assert len(debug) == 2                             # the repeats, quieter

    def test_a_frozen_signal_error_is_logged_once(self, tmp_path, caplog):
        frozen = ps.ABSENT_SIGNALS[0]
        write(tmp_path, "f_summary.json", signal=frozen, control_validated=True)
        out = call(tmp_path, caplog)
        assert out == [None, None, None]
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1 and "FROZEN" in errors[0].getMessage()

    def test_an_unreadable_artefact_warns_once(self, tmp_path, caplog):
        (tmp_path / "bad_summary.json").write_text("{not json")
        call(tmp_path, caplog)
        warned = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warned) == 1 and "unreadable edge artefact" in warned[0].getMessage()

    def test_an_edited_artefact_is_reported_again(self, tmp_path, caplog):
        path = write(tmp_path, "a_summary.json")
        call(tmp_path, caplog, times=2)
        st = path.stat()
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
        call(tmp_path, caplog, times=2)
        assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1

    def test_a_different_directory_is_its_own_scope(self, tmp_path, caplog):
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir(), b.mkdir()
        write(a, "x_summary.json")
        write(b, "x_summary.json")
        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger="project_status"):
            ps.cleared_edge_signal_from_artifacts(str(a))
            ps.cleared_edge_signal_from_artifacts(str(b))
        assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2

    def test_the_seen_set_is_bounded(self, monkeypatch):
        monkeypatch.setattr(ps, "_LOGGED_ONCE_CAP", 8)
        for i in range(50):
            ps._log_once(logging.DEBUG, ("d", str(i)), "m %s", i)
        assert len(ps._LOGGED_ONCE) <= 8

    def test_refusal_is_never_cached_only_the_log_line_is(self, tmp_path, caplog):
        """Every call still returns None for a refused artefact; deleting the
        offending file and calling again still answers from the files."""
        path = write(tmp_path, "bad_summary.json")
        assert call(tmp_path, caplog, times=2) == [None, None]
        path.unlink()
        assert ps.cleared_edge_signal_from_artifacts(str(tmp_path)) is None
