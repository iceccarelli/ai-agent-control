"""The ONE supported route for evaluating the frozen prospective plan: a durable, tamper-evident journal of every look.

Why it exists. ``statistical_power.TrialLedger`` lives in memory. Three evaluations with three fresh ledgers each report ``n_trials == 1``, and a ledger
rebuilt one entry shorter verifies, so the Holm correction inside ``carry_prospective.evaluate`` is only as honest as the ledger it is handed.
``carry_prospective.evaluate`` stays the PURE calculation (research tests call it with synthetic ledgers); this module is the operator route and
is the only one that evaluates a closed window: ``python tools/trial_journal.py evaluate ...``. ``carry_prospective``'s own CLI reports progress and
can never evaluate.

What it guarantees, and what it does not.
  * Every look is one hash-chained JSON line, replayed through ``TrialLedger.record`` on load so every hash is RECOMPUTED; no stored hash is trusted.
  * One exclusive ``flock`` on ``<journal>.lock`` is held across load -> evaluate -> append, so two evaluators cannot claim the same trial index.
    The size read under the lock is compared again just before the append (compare-and-append) so a writer that ignored the lock is detected, not
    silently interleaved. ``flock`` is advisory and is unreliable on network file systems: keep the journal on a local disk.
    The lock file is never deleted (removing it is itself a race). A crashed holder releases the lock when its process dies.
  * A write is looped to completion and fsynced; any failure raises and the look is NOT reported as made. A journal that may hold a partial line is
    never repaired or truncated here: the next load refuses it, preserving the evidence, until a human reconciles it.
  * Edits, reordering, a torn last line and a forged hash are caught with no extra input. Removal of the TAIL, or of the whole journal, is caught
    only against an ANCHOR (count + head hash) kept where the writer cannot reach: an anchor next to the journal protects against nothing, so an
    anchor file in the journal's directory is refused. Without an anchor the route refuses unless the operator says so explicitly, and then the
    report states the limit. A hash on the same disk is not tamper-proof storage.

Offline. The only files opened are the journal, its lock file and an operator-named anchor file. No network, process or database is touched.
"""
from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
import re
import stat
import sys
import time
from typing import Any, Dict, Optional, Sequence, Tuple

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BOT)
sys.path.insert(0, os.path.join(BOT, "tools"))

import account_review as AR  # noqa: E402  (its guarded-path rule is the one protected-name list)
import carry_prospective as CP  # noqa: E402
import statistical_power as SP  # noqa: E402

_FIELDS = ("strategy_id", "params_hash", "window_id", "statistic", "pvalue", "n_obs", "plan_digest")
_ROW_KEYS = set(_FIELDS) | {"index", "prev_hash", "entry_hash"}
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
MAX_JOURNAL_BYTES = 8 * 1024 * 1024
LOCK_TIMEOUT_S = 30.0

# anchor states (``inspect``)
NEVER_INITIALISED = "NEVER_INITIALISED"
VERIFIED_AT_ANCHOR = "VERIFIED_AT_ANCHOR"
VERIFIED_AHEAD_OF_ANCHOR = "VERIFIED_AHEAD_OF_ANCHOR"
UNANCHORED = "UNANCHORED"
SHORTENED = "SHORTENED"
REMOVED = "REMOVED"
REWRITTEN = "REWRITTEN"
CORRUPT = "CORRUPT"
OK_STATES = (VERIFIED_AT_ANCHOR, VERIFIED_AHEAD_OF_ANCHOR)

# the two calls fault-injection tests replace
_os_write = os.write
_os_fsync = os.fsync


class JournalError(SP.StatisticalPowerError):
    """The journal route refuses, and says why. Never a silent fallback to an empty ledger."""


class JournalLockError(JournalError):
    """Another evaluator holds the journal, or the lock file is unsafe."""


class JournalWriteError(JournalError):
    """An append failed part-way. The journal may hold a partial line: it is left untouched for a human to reconcile; this look was NOT made."""


# ------------------------------------------------------------------------------------------------ anchors

def parse_anchor(n: Any, head: Any) -> SP.LedgerAnchor:
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        raise JournalError("an anchor must name a count of at least 1: an anchor of 0 protects nothing")
    if not isinstance(head, str) or not _HEX64.match(head):
        raise JournalError("an anchor head must be 64 lowercase hex characters")
    return SP.LedgerAnchor(n, head)


def read_anchor_file(path: str, journal_path: str) -> SP.LedgerAnchor:
    """``<count> <head>`` on one line. Refused when it sits in the journal's own directory: it would be no independent protection."""
    if os.path.dirname(os.path.realpath(path)) == os.path.dirname(os.path.realpath(journal_path)):
        raise JournalError("the anchor file is in the journal's directory: whoever can rewrite the journal can rewrite that, so it protects nothing. "
                           "Keep the anchor somewhere the writer cannot reach and give it here from a copy outside that directory")
    AR._refuse_protected(path)
    fd = _open_regular(path, os.O_RDONLY, create=False)
    try:
        raw = os.read(fd, 4096).decode("utf-8", "strict")
    finally:
        os.close(fd)
    parts = raw.split()
    if len(parts) != 2 or not parts[0].isascii() or not parts[0].isdigit():
        raise JournalError("the anchor file must hold exactly '<count> <head>'")
    return parse_anchor(int(parts[0]), parts[1])


# ------------------------------------------------------------------------------------------------ files

def _open_regular(path: str, flags: int, *, create: bool, mode: int = 0o600) -> int:
    """Open without following a link, then judge the OPENED descriptor: a regular file, one link, owned by us, not writable by others."""
    parent = os.path.dirname(os.path.abspath(path)) or "."
    try:
        pst = os.lstat(parent)
    except OSError as exc:
        raise JournalError(f"cannot use the directory of {os.path.basename(path)}: {exc.strerror}") from exc
    if not stat.S_ISDIR(pst.st_mode):
        raise JournalError("the journal's directory must be a real directory, not a link")
    if os.path.islink(path):                                           # also a dangling link: lexists-based, so it cannot slip past an exists() test
        raise JournalError(f"{os.path.basename(path)} is a symbolic link: refused")
    try:
        fd = os.open(path, flags | os.O_NOFOLLOW | os.O_CLOEXEC | (os.O_CREAT if create else 0), mode)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise JournalError(f"{os.path.basename(path)} is a symbolic link: refused") from exc
        raise
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise JournalError(f"{os.path.basename(path)} is not a regular file")
        if st.st_nlink != 1:
            raise JournalError(f"{os.path.basename(path)} has {st.st_nlink} hard links: a hard link can give another file this name")
        if st.st_uid != os.geteuid():
            raise JournalError(f"{os.path.basename(path)} is not owned by the running user")
        if st.st_mode & 0o022:
            raise JournalError(f"{os.path.basename(path)} is writable by group or others (mode {stat.S_IMODE(st.st_mode):o})")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _read_all(fd: int, size: int) -> bytes:
    if size > MAX_JOURNAL_BYTES:
        raise JournalError("the journal is larger than any plausible record of looks: refused")
    out, off = [], 0
    while off < size:
        chunk = os.pread(fd, min(1 << 20, size - off), off)
        if not chunk:
            raise JournalError("the journal shrank while it was being read")
        out.append(chunk)
        off += len(chunk)
    return b"".join(out)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        n = _os_write(fd, view)
        if n <= 0:
            raise OSError(errno.EIO, "write made no progress")
        view = view[n:]


class _Lock:
    def __init__(self, journal_path: str, timeout_s: float) -> None:
        self.path, self.timeout_s, self.fd = journal_path + ".lock", timeout_s, -1

    def __enter__(self) -> "_Lock":
        AR._refuse_protected(self.path)
        self.fd = _open_regular(self.path, os.O_RDWR, create=True)
        deadline = time.monotonic() + self.timeout_s
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError as exc:
                if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                    os.close(self.fd)
                    raise JournalLockError(f"cannot lock the journal: {exc.strerror}") from exc
                if time.monotonic() >= deadline:
                    os.close(self.fd)
                    raise JournalLockError("another evaluation holds the journal lock: refused rather than risk two looks claiming one trial index") from exc
                time.sleep(0.05)

    def __exit__(self, *_a: Any) -> None:
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)


# ------------------------------------------------------------------------------------------------ replay

def _reject_constant(name: str) -> Any:
    raise SP.LedgerTamperError(f"journal holds the non-finite number {name}")


def _replay(raw: bytes) -> SP.TrialLedger:
    """Rebuild the ledger line by line; every hash is recomputed by ``record``. Raises LedgerTamperError on anything but a clean chain."""
    ledger = SP.TrialLedger()
    if not raw:
        return ledger
    try:
        text = raw.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise SP.LedgerTamperError("the journal is not UTF-8") from exc
    if not text.endswith("\n"):
        raise SP.LedgerTamperError("the journal ends in a partial line: an interrupted write or an edit. It is left untouched; copy it aside and reconcile it before any evaluation")
    for i, line in enumerate(text.split("\n")[:-1]):
        try:
            row = json.loads(line, parse_constant=_reject_constant)
        except ValueError as exc:
            raise SP.LedgerTamperError(f"journal line {i + 1} is not JSON") from exc
        if not isinstance(row, dict) or set(row) != _ROW_KEYS:
            raise SP.LedgerTamperError(f"journal line {i + 1} does not have the registered fields")
        try:
            trial = ledger.record(row["strategy_id"], row["params_hash"], row["window_id"], row["statistic"], pvalue=row["pvalue"], n_obs=row["n_obs"],
                                  plan_digest=row["plan_digest"])
        except SP.StatisticalPowerError as exc:
            raise SP.LedgerTamperError(f"journal line {i + 1} holds a value the ledger refuses: {exc}") from exc
        if trial.index != row["index"] or trial.prev_hash != row["prev_hash"] or trial.entry_hash != row["entry_hash"]:
            raise SP.LedgerTamperError(f"journal line {i + 1} does not match the hash chain recomputed from the lines before it")
    return ledger


def _state(ledger: Optional[SP.TrialLedger], existed: bool, anchor: Optional[SP.LedgerAnchor], problem: Optional[str]) -> Tuple[str, str]:
    if problem is not None:
        return CORRUPT, problem
    if not existed:
        if anchor is not None:
            return REMOVED, f"no journal exists but an anchor says {anchor.n} look(s) were accepted: the journal was removed"
        return NEVER_INITIALISED, "no journal and no anchor: nothing has ever been recorded here"
    assert ledger is not None
    if anchor is None:
        return UNANCHORED, ("the journal verifies internally, but with no external anchor, removal of its last entries (or a rebuild from the start) cannot be detected")
    n = ledger.n_trials
    if n < anchor.n:
        return SHORTENED, f"the journal holds {n} look(s) but the anchor says {anchor.n} were accepted"
    if ledger.trials[anchor.n - 1].entry_hash != anchor.head:
        return REWRITTEN, f"the journal's entry {anchor.n} does not match the anchored head: the history was rewritten"
    if n == anchor.n:
        return VERIFIED_AT_ANCHOR, "the journal ends exactly at the anchor"
    return VERIFIED_AHEAD_OF_ANCHOR, f"the journal extends the anchor by {n - anchor.n} look(s) the anchor does not record: record the new anchor"


def inspect(journal_path: str, anchor: Optional[SP.LedgerAnchor] = None) -> Dict[str, Any]:
    """Read-only judgement of the journal against an anchor. Takes no lock and writes nothing; use it to validate, not to evaluate."""
    AR._refuse_protected(journal_path)
    ledger, problem, existed = None, None, os.path.lexists(journal_path)
    if existed:
        fd = _open_regular(journal_path, os.O_RDONLY, create=False)
        try:
            raw = _read_all(fd, os.fstat(fd).st_size)
        finally:
            os.close(fd)
        try:
            ledger = _replay(raw)
        except SP.LedgerTamperError as exc:
            problem = str(exc)
    status, why = _state(ledger, existed, anchor, problem)
    n = ledger.n_trials if ledger is not None else 0
    return {"status": status, "why": why, "n_trials": n, "head": ledger.head if ledger is not None and n else None, "anchor_checked": anchor is not None,
            "ok_to_evaluate": status in OK_STATES}


# ------------------------------------------------------------------------------------------------ the operator route

def evaluate_logged(prints: Sequence[Tuple[float, float]], hurdle_pct: float, journal_path: str, *, anchor: Optional[SP.LedgerAnchor] = None,
                    first_look: bool = False, allow_unanchored: bool = False, now_ms: Optional[int] = None,
                    lock_timeout_s: float = LOCK_TIMEOUT_S) -> Dict[str, Any]:
    """``carry_prospective.evaluate`` with the trial ledger loaded from, and the new look appended to, the journal, all under one lock.

    Refuses (nothing is appended, nothing falls back to an empty in-memory ledger) when: the journal is corrupt, shortened, removed or rewritten;
    an anchor exists and is not met; a journal exists and no anchor was given (unless ``allow_unanchored``); nothing was ever recorded and the
    operator did not say ``first_look``. ``now_ms`` can only move the clock earlier (see ``carry_prospective.evaluate``)."""
    AR._refuse_protected(journal_path)
    with _Lock(journal_path, lock_timeout_s):
        existed = os.path.lexists(journal_path)
        fd = _open_regular(journal_path, os.O_RDWR | os.O_APPEND, create=False) if existed else -1
        try:
            size = os.fstat(fd).st_size if existed else 0
            problem, ledger = None, None
            try:
                ledger = _replay(_read_all(fd, size)) if existed else SP.TrialLedger()
            except SP.LedgerTamperError as exc:
                problem = str(exc)
            status, why = _state(ledger, existed, anchor, problem)
            if status == NEVER_INITIALISED:
                if not first_look:
                    raise JournalError("no journal exists and no anchor was given: if this really is the first look at the registered window, say so explicitly (first_look); "
                                       "otherwise the journal was removed and the anchor must be supplied")
            elif status == UNANCHORED:
                if not allow_unanchored:
                    raise JournalError("a journal exists but no anchor was given: " + why + ". Supply the last anchor, or explicitly accept the limit (allow_unanchored)")
            elif status not in OK_STATES:
                raise JournalError(f"{status}: {why}. The journal was not touched; keep it as evidence")
            assert ledger is not None
            before = ledger.n_trials
            rep = CP.evaluate(prints, hurdle_pct, ledger, now_ms)           # the pure calculation, unchanged; it raises (and records nothing) when it refuses
            new = ledger.trials[before:]
            lines = "".join(json.dumps({"index": t.index, **{k: getattr(t, k) for k in _FIELDS}, "prev_hash": t.prev_hash, "entry_hash": t.entry_hash},
                                       sort_keys=True, allow_nan=False) + "\n" for t in new).encode("utf-8")
            created = not existed
            if created:
                fd = _open_regular(journal_path, os.O_RDWR | os.O_APPEND, create=True)
                if os.fstat(fd).st_size != 0:                               # a writer ignoring the lock created it first
                    raise JournalError("the journal appeared while it was held under the lock: refused (a writer ignored the lock)")
            elif os.fstat(fd).st_size != size:                              # compare-and-append: someone wrote without taking the lock
                raise JournalError("the journal changed while it was held under the lock: refused (a writer ignored the lock)")
            try:
                _write_all(fd, lines)
                _os_fsync(fd)
            except OSError as exc:
                raise JournalWriteError(f"the append failed ({exc.strerror or exc}); the journal may hold a partial line and was not repaired. "
                                        "This look was NOT made. Copy the journal aside and reconcile it before any evaluation") from exc
            if created:
                dfd = os.open(os.path.dirname(os.path.abspath(journal_path)) or ".", os.O_RDONLY)
                try:
                    _os_fsync(dfd)
                finally:
                    os.close(dfd)
        finally:
            if fd >= 0:
                os.close(fd)
    a = ledger.anchor()
    rep = dict(rep)
    rep["journal"] = {"state_before": status, "n_trials": a.n, "head": a.head, "record_this_anchor_outside_the_journal": {"n": a.n, "head": a.head},
                      "checked_against_an_anchor": anchor is not None,
                      "limit": None if anchor is not None else
                      "NO ANCHOR WAS CHECKED: edits and reordering are caught, but removal of the journal's tail, or of the whole journal, is not"}
    return rep


# ------------------------------------------------------------------------------------------------ CLI

def _anchor_from_args(a: argparse.Namespace) -> Optional[SP.LedgerAnchor]:
    if a.anchor_file and (a.anchor_n is not None or a.anchor_head):
        raise JournalError("give the anchor either as --anchor-file or as --anchor-n and --anchor-head, not both")
    if a.anchor_file:
        return read_anchor_file(a.anchor_file, a.journal)
    if a.anchor_n is not None or a.anchor_head:
        if a.anchor_n is None or not a.anchor_head:
            raise JournalError("--anchor-n and --anchor-head go together")
        return parse_anchor(a.anchor_n, a.anchor_head)
    return None


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Durable journal of looks at the frozen prospective evaluation. Offline; no request, no order.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("verify", "evaluate"):
        p = sub.add_parser(name)
        p.add_argument("--journal", required=True, help="local journal file (kept on a local disk, not a network share)")
        p.add_argument("--anchor-n", type=int, default=None)
        p.add_argument("--anchor-head", default=None)
        p.add_argument("--anchor-file", default=None, help="file holding '<count> <head>', kept OUTSIDE the journal's directory")
        if name == "evaluate":
            p.add_argument("--funding", required=True, help="local .jsonl/.csv of settled prints: ts_ms, funding_bps")
            p.add_argument("--hurdle-pct", type=float, default=10.0)
            p.add_argument("--first-look", action="store_true", help="nothing has ever been recorded: create the journal")
            p.add_argument("--allow-unanchored", action="store_true", help="evaluate without an external anchor (the report states the limit)")
    a = ap.parse_args(argv)
    try:
        anchor = _anchor_from_args(a)
        if a.cmd == "verify":
            rep = inspect(a.journal, anchor)
            print(json.dumps(rep, indent=2, sort_keys=True))
            return 0 if rep["ok_to_evaluate"] else 3
        rep = evaluate_logged(CP._load(a.funding), a.hurdle_pct, a.journal, anchor=anchor, first_look=a.first_look, allow_unanchored=a.allow_unanchored)
    except (JournalError, SP.StatisticalPowerError, AR.RecordError, OSError) as exc:
        print(f"trial_journal: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(rep, indent=2, sort_keys=True, allow_nan=False, default=str))
    j = rep["journal"]
    print(f"\nRECORD THIS ANCHOR OUTSIDE THE JOURNAL'S HOST NOW:  {j['n_trials']} {j['head']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
