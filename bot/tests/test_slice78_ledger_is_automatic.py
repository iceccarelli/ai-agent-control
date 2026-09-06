"""Slice 78 — the read ledger must not depend on anyone remembering.

Slice 77 built `reserved_holdout` and seeded it BY HAND. That caught the
slice-57 contamination retroactively, which is worth something, but a ledger
maintained by hand is a good intention, not an invariant. Good intentions are
exactly what failed in slice 57: every procedural guard passed, and the one
thing nobody checked was whether the bytes had been read before.

These tests make the recording automatic and keep it that way.
"""
from __future__ import annotations

import ast
import os
import sys

import pytest

REPO = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(REPO, "tools"))
sys.path.insert(0, REPO)

import market_data as md  # noqa: E402
import reserved_holdout as rh  # noqa: E402


class TestTheObserverRespectsIntegrationMap:
    def test_market_data_does_not_import_the_ledger(self):
        """§1: dependencies point downward. The ledger reaches UP, never down."""
        with open(os.path.join(REPO, "market_data.py"), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert "reserved_holdout" not in imported
        assert imported <= {
            "csv", "gzip", "hashlib", "io", "json", "logging", "math", "os",
            "re", "dataclasses", "typing", "numpy", "config", "__future__",
        }, f"unexpected imports: {sorted(imported)}"

    def test_the_observer_defaults_to_none(self):
        """Unwired, market_data records nothing and knows no one."""
        with open(os.path.join(REPO, "market_data.py"), encoding="utf-8") as fh:
            body = fh.read()
        assert "READ_OBSERVER = None" in body

    def test_install_sets_the_observer(self):
        previous = md.READ_OBSERVER
        try:
            md.READ_OBSERVER = None
            rh.install(md, reader="test", path="/tmp/t78_install.json")
            assert md.READ_OBSERVER is not None
        finally:
            md.READ_OBSERVER = previous


class TestRecordingIsSafeUnderFailure:
    def test_recording_is_idempotent(self, tmp_path, monkeypatch):
        """Re-running a measurement is not a second look at the data."""
        monkeypatch.delenv(rh.DISABLE_ENV, raising=False)  # conftest sets it
        path = str(tmp_path / "L.json")
        kw = dict(dataset="X", from_utc="2026-01-01T00:00:00Z",
                  to_utc="2026-02-01T00:00:00Z", read_by="t", path=path)
        assert rh.auto_record(**kw) is True
        assert rh.auto_record(**kw) is False

    def test_an_unwritable_ledger_never_raises(self):
        """A measurement must not die at its last line. Slice 77 fixed that once."""
        assert rh.auto_record(dataset="X", from_utc="2026-01-01T00:00:00Z",
                              to_utc="2026-02-01T00:00:00Z", read_by="t",
                              path="/proc/1/no/L.json") is False

    def test_a_malformed_timestamp_never_raises(self):
        assert rh.auto_record(dataset="X", from_utc="not-a-time",
                              to_utc="2026-02-01T00:00:00Z", read_by="t",
                              path="/tmp/t78_bad.json") is False

    def test_the_kill_switch_suppresses_recording(self, tmp_path, monkeypatch):
        monkeypatch.setenv(rh.DISABLE_ENV, "1")
        assert rh.auto_record(dataset="X", from_utc="2026-01-01T00:00:00Z",
                              to_utc="2026-02-01T00:00:00Z", read_by="t",
                              path=str(tmp_path / "L.json")) is False


class TestEveryCorpusReaderInstallsTheLedger:
    """The invariant. A tool that reads corpora and does not record is a hole.

    This is the slice-78 equivalent of the AST test that forbids assigning
    `live_authorized`: it does not ask anyone to be careful, it fails the build.
    """

    #: Historical slice tools are digest-pinned provenance records (16 of them)
    #: and MUST stay byte-identical, so they cannot install the hook. They are
    #: archival, they are not run against new data, and rewriting them to add a
    #: call would retroactively falsify how past measurements were produced.
    ARCHIVAL_PREFIXES = ("slice6", "slice7")

    def _tools_that_load_corpora(self):
        tools_dir = os.path.join(REPO, "tools")
        for name in sorted(os.listdir(tools_dir)):
            if not name.endswith(".py"):
                continue
            if name.startswith(self.ARCHIVAL_PREFIXES):
                continue
            with open(os.path.join(tools_dir, name), encoding="utf-8") as fh:
                body = fh.read()
            if "load_corpus" in body:
                yield name, body

    def test_at_least_one_tool_is_checked(self):
        assert list(self._tools_that_load_corpora()), "the scan found nothing"

    def test_current_corpus_readers_install_the_ledger(self):
        missing = [name for name, body in self._tools_that_load_corpora()
                   if "reserved_holdout" not in body]
        assert not missing, (
            "these tools read a corpus without recording the read: "
            f"{missing}. Add `import reserved_holdout; reserved_holdout.install()` "
            "before the first load_corpus call, or the next holdout cannot be "
            "certified untouched.")
