"""The ONE supported route for evaluating the frozen prospective plan: a durable, locked, hash-chained journal of every look.

Why it exists. ``statistical_power.TrialLedger`` lives in memory. Three evaluations with three fresh ledgers each report ``n_trials == 1``, and a ledger
rebuilt one entry shorter verifies, so the Holm correction inside ``carry_prospective.evaluate`` is only as honest as the ledger it is handed.
``carry_prospective.evaluate`` stays the PURE calculation (research tests call it with synthetic ledgers); this module is the operator route and
is the only one that evaluates a closed window: ``python tools/trial_journal.py evaluate ...``. ``carry_prospective``'s own CLI reports progress and
can never evaluate. (Nothing can stop someone running the pure function with a throwaway ledger to PREVIEW a result: such a look is not counted.)

What the code enforces, and what it cannot.
  * Every look is one JSON line, hash-chained; the ledger is replayed through ``TrialLedger.record`` on load so every hash is RECOMPUTED. The hashes are
    plain unkeyed SHA-256: they detect accidental damage and naive edits, NOT a writer who rebuilds a consistent journal. Anyone who can write the
    journal can delete it and rebuild a perfectly valid one (``--first-look`` on an empty directory), and it will verify as UNANCHORED.
  * One exclusive ``flock`` on ``<journal>.lock`` is held across load -> evaluate -> append, so two evaluators cannot claim the same trial index.
    The size and inode read under the lock are compared again just before the append (compare-and-append). ``flock`` is advisory and is unreliable on
    network file systems: keep the journal on a local disk. The lock file is never deleted (removing it is itself a race).
  * A write is looped to completion and fsynced; any failure raises and the look is not reported. A journal that may hold a partial line is never
    repaired or truncated here: the next load refuses it, preserving the evidence, until a human reconciles it.
  * Removal of the TAIL, a rebuilt journal, or deletion of the whole file is detected ONLY against an ANCHOR (count + head hash) that is (a) held by
    someone the journal's writer cannot influence AND (b) the LATEST one. This module cannot know either. What it checks is only that the anchor file
    is not in the journal's own directory, which is a string comparison and NOT a trust boundary: a different directory, a subdirectory, or a file
    typed on the command line from shell history all pass. A STALE anchor verifies a truncated journal (3 looks, cut to 1, checked against the n=1
    anchor: VERIFIED_AT_ANCHOR) and the evaluation then records 2 looks instead of 4. Custody and freshness are the operator's procedure
    (see ``main`` output), not something enforced here.

Offline. The only files opened are the journal, its lock file and an operator-named anchor file. No network, process or database is touched.
"""
from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import math
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
_ROW_KEYS = set(_FIELDS) | {"index", "prev_hash", "entry_hash", "data", "line_hash"}
_DATA_KEYS = {"sha256", "n_prints", "hurdle_pct", "revision"}
_LINE_GENESIS = hashlib.sha256(b"trial_journal.lines.v2").hexdigest()
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
MAX_JOURNAL_BYTES = 8 * 1024 * 1024
LOCK_TIMEOUT_S = 30.0
MAX_FLOOR_BPS = 1000.0

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
                           "Give the anchor from a record held by someone the journal's writer cannot influence (this check only compares directories; it is not independent custody)")
    AR._refuse_protected(path)
    fd = _open_regular(path, os.O_RDONLY, create=False)
    try:
        data = os.read(fd, 4097)
        if len(data) > 4096:
            raise JournalError("the anchor file is larger than any anchor: refused")
        try:
            raw = data.decode("utf-8", "strict")
        except UnicodeDecodeError as exc:
            raise JournalError("the anchor file is not UTF-8") from exc
    finally:
        os.close(fd)
    parts = raw.split()
    if len(parts) != 2 or not parts[0].isascii() or not parts[0].isdigit():
        raise JournalError("the anchor file must hold exactly '<count> <head>'")
    return parse_anchor(int(parts[0]), parts[1])


# ------------------------------------------------------------------------------------------------ files

def _open_regular(path: str, flags: int, *, create: bool, mode: int = 0o600) -> int:
    """Open without following a link and without blocking, then judge the OPENED descriptor: a regular file, one link, owned by us, not writable by
    others. The directory is opened once (no-follow, directory-only) and the file is opened relative to THAT descriptor, so an ancestor swapped for a
    link after the check cannot redirect the open; the protected-name rule is then re-applied to where the descriptor really points."""
    parent, name = os.path.split(os.path.abspath(path))
    try:
        pst = os.lstat(parent)
    except OSError as exc:
        raise JournalError(f"cannot use the directory of {name}: {exc.strerror}") from exc
    if not stat.S_ISDIR(pst.st_mode):
        raise JournalError("the journal's directory must be a real directory, not a link")
    try:
        pfd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise JournalError(f"cannot use the directory of {name}: {exc.strerror}") from exc
    try:
        try:
            where = os.readlink(f"/proc/self/fd/{pfd}")
        except OSError:
            where = parent
        AR._refuse_protected(os.path.join(where, name))                  # the directory the descriptor really names, not the one that was named
        try:
            if stat.S_ISLNK(os.lstat(name, dir_fd=pfd).st_mode):         # also a dangling link: lexists-based, so it cannot slip past an exists() test
                raise JournalError(f"{name} is a symbolic link: refused")
        except FileNotFoundError:
            pass
        try:
            fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC | (os.O_CREAT if create else 0), mode, dir_fd=pfd)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise JournalError(f"{name} is a symbolic link: refused") from exc
            raise
    finally:
        os.close(pfd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):                                 # a FIFO/device was opened non-blocking and is refused here, before any read
            raise JournalError(f"{name} is not a regular file")
        if st.st_nlink != 1:
            raise JournalError(f"{name} has {st.st_nlink} hard links: a hard link can give another file this name")
        if st.st_uid != os.geteuid():
            raise JournalError(f"{name} is not owned by the running user")
        if st.st_mode & 0o022:
            raise JournalError(f"{name} is writable by group or others (mode {stat.S_IMODE(st.st_mode):o})")
        fcntl.fcntl(fd, fcntl.F_SETFL, fcntl.fcntl(fd, fcntl.F_GETFL) & ~os.O_NONBLOCK)
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
        deadline = time.monotonic() + self.timeout_s
        while True:
            self.fd = _open_regular(self.path, os.O_RDWR, create=True)
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                os.close(self.fd)
                if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                    raise JournalLockError(f"cannot lock the journal: {exc.strerror}") from exc
                if time.monotonic() >= deadline:
                    raise JournalLockError("another evaluation holds the journal lock: refused rather than risk two looks claiming one trial index") from exc
                time.sleep(0.05)
                continue
            if _same_inode(self.fd, self.path):                          # the lock file was not unlinked/replaced while we waited: we lock what others lock
                return self
            os.close(self.fd)                                            # a stale inode: lock the file that is actually at the path

    def __exit__(self, *_a: Any) -> None:
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)


def _same_inode(fd: int, path: str) -> bool:
    try:
        a, b = os.fstat(fd), os.lstat(path)
    except OSError:
        return False
    return (a.st_ino, a.st_dev) == (b.st_ino, b.st_dev)


# ------------------------------------------------------------------------------------------------ replay

def _reject_constant(name: str) -> Any:
    raise SP.LedgerTamperError(f"journal holds the non-finite number {name}")


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _line_hash(prev_line: str, entry_hash: str, data: Dict[str, Any]) -> str:
    return hashlib.sha256((prev_line + "|" + entry_hash + "|" + _canon(data)).encode("utf-8")).hexdigest()


def data_binding(prints: Sequence[Tuple[float, float]], hurdle_pct: float, revision: bool) -> Dict[str, Any]:
    """What was looked at: a SHA-256 of the canonical prints (sorted ts and the exact value), their count and the hurdle. It is chained into the journal."""
    rows = sorted((int(t), repr(float(v))) for t, v in prints)
    return {"sha256": hashlib.sha256(_canon(rows).encode("utf-8")).hexdigest(), "n_prints": len(rows), "hurdle_pct": float(hurdle_pct), "revision": bool(revision)}


class _Replayed:
    def __init__(self) -> None:
        self.ledger = SP.TrialLedger()
        self.line_heads: list = []
        self.datas: list = []

    @property
    def head(self) -> Optional[str]:
        return self.line_heads[-1] if self.line_heads else None


def _replay(raw: bytes) -> _Replayed:
    """Rebuild the ledger line by line; every hash is recomputed by ``record`` and the line chain (which also binds the data digest) is recomputed here.
    Raises LedgerTamperError on anything but a clean chain."""
    out = _Replayed()
    if not raw:
        return out
    try:
        text = raw.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise SP.LedgerTamperError("the journal is not UTF-8") from exc
    if not text.endswith("\n"):
        raise SP.LedgerTamperError("the journal ends in a partial line: an interrupted write or an edit. It is left untouched; copy it aside and reconcile it before any evaluation")
    prev_line = _LINE_GENESIS
    for i, line in enumerate(text.split("\n")[:-1]):
        try:
            row = json.loads(line, parse_constant=_reject_constant)
        except ValueError as exc:
            raise SP.LedgerTamperError(f"journal line {i + 1} is not JSON") from exc
        if not isinstance(row, dict) or set(row) != _ROW_KEYS or not isinstance(row["data"], dict) or set(row["data"]) != _DATA_KEYS:
            raise SP.LedgerTamperError(f"journal line {i + 1} does not have the registered fields")
        try:
            trial = out.ledger.record(row["strategy_id"], row["params_hash"], row["window_id"], row["statistic"], pvalue=row["pvalue"], n_obs=row["n_obs"],
                                      plan_digest=row["plan_digest"])
            lh = _line_hash(prev_line, trial.entry_hash, row["data"])
        except (SP.StatisticalPowerError, ValueError, TypeError) as exc:
            raise SP.LedgerTamperError(f"journal line {i + 1} holds a value the ledger refuses: {exc}") from exc
        if trial.index != row["index"] or trial.prev_hash != row["prev_hash"] or trial.entry_hash != row["entry_hash"] or lh != row["line_hash"]:
            raise SP.LedgerTamperError(f"journal line {i + 1} does not match the hash chain recomputed from the lines before it")
        prev_line = lh
        out.line_heads.append(lh)
        out.datas.append(row["data"])
    return out


def _state(rp: Optional[_Replayed], existed: bool, anchor: Optional[SP.LedgerAnchor], problem: Optional[str]) -> Tuple[str, str]:
    if problem is not None:
        return CORRUPT, problem
    if not existed:
        if anchor is not None:
            return REMOVED, f"no journal exists but an anchor says {anchor.n} look(s) were accepted: the journal was removed"
        return NEVER_INITIALISED, "no journal and no anchor: nothing has ever been recorded here"
    assert rp is not None
    if not rp.line_heads and anchor is None:                             # a 0-byte file (a failed first append) records nothing: it is not a history
        return NEVER_INITIALISED, "the journal file is empty and no anchor exists: nothing has ever been recorded here"
    if anchor is None:
        return UNANCHORED, ("the journal verifies internally, but with no external anchor, removal of its last entries (or a rebuild from the start) cannot be detected")
    n = len(rp.line_heads)
    if n < anchor.n:
        return SHORTENED, f"the journal holds {n} look(s) but the anchor says {anchor.n} were accepted"
    if rp.line_heads[anchor.n - 1] != anchor.head:
        return REWRITTEN, f"the journal's entry {anchor.n} does not match the anchored head: the history was rewritten"
    if n == anchor.n:
        return VERIFIED_AT_ANCHOR, "the journal ends exactly at the anchor"
    return VERIFIED_AHEAD_OF_ANCHOR, f"the journal extends the anchor by {n - anchor.n} look(s) the anchor does not record: record the new anchor"


def _read_journal(journal_path: str) -> bytes:
    fd = _open_regular(journal_path, os.O_RDONLY, create=False)
    try:
        return _read_all(fd, os.fstat(fd).st_size)
    finally:
        os.close(fd)


def inspect(journal_path: str, anchor: Optional[SP.LedgerAnchor] = None) -> Dict[str, Any]:
    """Judgement of the journal against an anchor; writes nothing. A last line that looks partial is re-read once under the writer lock (when a lock file
    exists) before it is reported as corrupt, so a reader racing a writer's append does not raise a false alarm."""
    AR._refuse_protected(journal_path)
    rp, problem, existed = None, None, os.path.lexists(journal_path)
    if existed:
        raw = _read_journal(journal_path)
        try:
            rp = _replay(raw)
        except SP.LedgerTamperError as first:
            problem = str(first)
            if raw and not raw.endswith(b"\n") and os.path.lexists(journal_path + ".lock"):
                try:
                    with _Lock(journal_path, 5.0):
                        raw = _read_journal(journal_path)
                    rp, problem = _replay(raw), None
                except SP.LedgerTamperError as again:
                    problem = str(again)
                except JournalLockError:
                    pass
    status, why = _state(rp, existed, anchor, problem)
    n = len(rp.line_heads) if rp is not None else 0
    return {"status": status, "why": why, "n_trials": n, "head": rp.head if rp is not None and n else None, "anchor_checked": anchor is not None,
            "ok_to_evaluate": status in OK_STATES}


# ------------------------------------------------------------------------------------------------ the operator route

def _selection_sensitivity(prints: Sequence[Tuple[float, float]], hurdle_pct: float, raw: bytes, now_ms: Optional[int], registered: Dict[str, Any],
                           venue_floor_bps: Optional[float] = None) -> Dict[str, Any]:
    """REPORT ONLY; it changes no verdict, and it is NOT a worst-case bound.

    The registered test tolerates missing settlements down to the coverage floor, and a missing settlement is not random if the operator chose which to
    leave out. This re-runs the same test on throwaway copies of the ledger with every missing settlement filled by ONE value:
      * the LOWEST OBSERVED print (``lowest_observed_*``). What it shows: whether the verdict survives the missing prints being as low as the lowest one
        you supplied. What it does NOT show: robustness. An unobserved settlement can legally print far BELOW every observed one, so 'not flipped' here
        is not evidence that the verdict is safe against missing data (reproduced: SUPPORTED at the lowest observed fill, flipped at -2 bps);
      * the BREAK-EVEN fill (``breakeven_fill_bps``): the single value the missing settlements could all have taken for the registered verdict to stop
        being supported (bisection, assuming the verdict is monotone in the fill and searching 50 bps below the lowest observed print). Read it
        against what you know funding can print; this module has no venue floor and invents none;
      * an OWNER-SUPPLIED floor (``venue_floor_*``), only when given: the same test with the missing settlements filled at that value."""
    have = {int(t): float(v) for t, v in prints}
    grid = [CP.WINDOW_START_MS + i * CP.SETTLE_MS for i in range(CP.WINDOW_DAYS * CP.PER_DAY)]
    missing = [t for t in grid if t not in have]
    supported = "SUPPORTED" in str(registered.get("verdict"))
    out: Dict[str, Any] = {"method": "FILL OF MISSING SETTLEMENTS WITH ONE VALUE - a sensitivity check, NOT a worst-case bound", "missing_settlements": len(missing),
                           "registered_verdict": registered.get("verdict")}
    if not missing:
        out.update(flipped_by_lowest_observed_fill=False, breakeven_fill_bps=None, breakeven_status="NOTHING_MISSING",
                   note="no settlement is missing: nothing could have been left out")
        return out
    low = min(have.values())

    def verdict_at(fill: float) -> str:
        shadow = _replay(raw).ledger
        return str(CP.evaluate([(t, have.get(t, fill)) for t in grid], hurdle_pct, shadow, now_ms).get("verdict"))
    try:
        out["lowest_observed_fill_bps"] = low
        out["lowest_observed_fill_verdict"] = verdict_at(low)
        out["flipped_by_lowest_observed_fill"] = bool(supported and "SUPPORTED" not in out["lowest_observed_fill_verdict"])
        out["breakeven_status"] = "REGISTERED_VERDICT_NOT_SUPPORTED"
        out["breakeven_fill_bps"] = None
        if supported:
            lo_fill = low - 50.0
            if "SUPPORTED" in verdict_at(lo_fill):
                out["breakeven_fill_bps"] = None
                out["breakeven_status"] = "STILL_SUPPORTED_50_BPS_BELOW_LOWEST_OBSERVED"
                out["breakeven_note"] = "still supported even with every missing settlement 50 bps below the lowest observed print"
            elif out["flipped_by_lowest_observed_fill"]:
                out["breakeven_fill_bps"] = low
                out["breakeven_status"] = "ALREADY_FLIPPED_AT_LOWEST_OBSERVED"
                out["breakeven_note"] = "already not supported at the lowest observed print"
            else:
                hi, lo_ = low, lo_fill
                for _ in range(24):
                    mid = (hi + lo_) / 2.0
                    if "SUPPORTED" in verdict_at(mid):
                        hi = mid
                    else:
                        lo_ = mid
                out["breakeven_fill_bps"] = hi
                out["breakeven_status"] = "FLIPS_AT"
                out["breakeven_note"] = ("the registered verdict stops being supported if the missing settlements averaged about this (assuming a monotone verdict); "
                                         "compare it with what funding can legally print")
        if venue_floor_bps is not None:
            out["venue_floor_fill_bps"] = float(venue_floor_bps)
            out["venue_floor_source"] = "OWNER_SUPPLIED_UNVERIFIED"
            out["venue_floor_fill_verdict"] = verdict_at(float(venue_floor_bps))
        out["note"] = ("DO NOT RELY: the registered verdict is 'supported' only because of which settlements are present" if out["flipped_by_lowest_observed_fill"] else
                       "NOT a worst case: unobserved settlements could legally print below the lowest observed one. 'Not flipped' does NOT show the verdict is robust "
                       "to missing data; read breakeven_fill_bps")
    except SP.StatisticalPowerError as exc:
        out.update(flipped_by_lowest_observed_fill=None, breakeven_fill_bps=None, breakeven_status="NOT_EVALUATED",
                   note=f"the sensitivity check could not be evaluated: {exc}")
    return out


MAX_PLAUSIBLE_ABS_BPS = 300.0        # the same per-settlement bound prospective_prints applies to the raw venue value
MIN_PLAUSIBLE_MEDIAN_ABS_BPS = 0.01  # the venue's own baseline is 1 bp; a series whose typical print is 1/100 of that is fractions, not basis points
MIN_UNITS_SAMPLE = 30


def screen_units(prints: Sequence[Tuple[float, float]]) -> None:
    """Refuses a prints file that is evidently in the wrong unit BEFORE a look is spent on it. The registered evaluator reads ``funding_bps`` without any
    unit check, so a file in fractions (÷10^4) silently reads as 'no edge' and a file in 10^-8 units reads as 'insufficient power', and either is journaled
    as a look. ``tools/prospective_prints.py`` is the producer that cannot make this mistake; this is the last line for a hand-made file."""
    vals = [float(v) for _t, v in prints]
    if any(not math.isfinite(v) for v in vals):
        raise JournalError("the prints hold a non-finite funding value")
    worst = max((abs(v) for v in vals), default=0.0)
    if worst > MAX_PLAUSIBLE_ABS_BPS:
        raise JournalError(f"a funding print of {worst:g} bps is not a plausible per-settlement rate (bound {MAX_PLAUSIBLE_ABS_BPS:g}): wrong unit or a corrupt row; "
                           "nothing is journaled. Make the file with tools/prospective_prints.py")
    if len(vals) >= MIN_UNITS_SAMPLE:
        mid = sorted(abs(v) for v in vals)[len(vals) // 2]
        if mid < MIN_PLAUSIBLE_MEDIAN_ABS_BPS:
            raise JournalError(f"the median |funding| is {mid:g} bps, 1/100 of the venue's 1 bp baseline or less: this looks like a file in FRACTIONS, not basis points; "
                               "nothing is journaled. Make the file with tools/prospective_prints.py")


def evaluate_logged(prints: Sequence[Tuple[float, float]], hurdle_pct: float, journal_path: str, *, anchor: Optional[SP.LedgerAnchor] = None,
                    first_look: bool = False, allow_unanchored: bool = False, accept_data_revision: bool = False, now_ms: Optional[int] = None,
                    lock_timeout_s: float = LOCK_TIMEOUT_S, venue_floor_bps: Optional[float] = None) -> Dict[str, Any]:
    """``carry_prospective.evaluate`` with the trial ledger loaded from, and the new look appended to, the journal, all under one lock.

    Refuses (nothing is appended, nothing falls back to an empty in-memory ledger) when: the journal is corrupt, shortened, removed or rewritten;
    an anchor exists and is not met; a journal exists and no anchor was given (unless ``allow_unanchored``); nothing was ever recorded and the
    operator did not say ``first_look``; or the prints differ from those of the FIRST look (unless ``accept_data_revision``, which is itself recorded in
    the chain). Each look's data digest is chained, so a changed or reduced data set can be seen afterwards. ``now_ms`` can only move the clock earlier."""
    AR._refuse_protected(journal_path)
    if venue_floor_bps is not None and (isinstance(venue_floor_bps, bool) or not isinstance(venue_floor_bps, (int, float))
                                        or not math.isfinite(venue_floor_bps) or abs(venue_floor_bps) > MAX_FLOOR_BPS):
        # Checked BEFORE the look is journaled: a closed-window look is one-shot, and an unusable report option must never spend it.
        raise JournalError(f"the venue funding floor must be a finite number of bps within +/-{MAX_FLOOR_BPS:g}, not {venue_floor_bps!r}")
    screen_units(prints)
    binding = data_binding(prints, hurdle_pct, False)
    with _Lock(journal_path, lock_timeout_s):
        existed = os.path.lexists(journal_path)
        fd = _open_regular(journal_path, os.O_RDWR | os.O_APPEND, create=False) if existed else -1
        try:
            size = os.fstat(fd).st_size if existed else 0
            problem, rp, raw = None, None, b""
            try:
                raw = _read_all(fd, size) if existed else b""
                rp = _replay(raw)
            except SP.LedgerTamperError as exc:
                problem = str(exc)
            status, why = _state(rp, existed, anchor, problem)
            if status == NEVER_INITIALISED:
                if not first_look:
                    raise JournalError("no journal exists and no anchor was given: if this really is the first look at the registered window, say so explicitly (first_look); "
                                       "otherwise the journal was removed and the anchor must be supplied")
            elif status == UNANCHORED:
                if not allow_unanchored:
                    raise JournalError("a journal exists but no anchor was given: " + why + ". Supply the last anchor, or explicitly accept the limit (allow_unanchored)")
            elif status not in OK_STATES:
                raise JournalError(f"{status}: {why}. The journal was not touched; keep it as evidence")
            assert rp is not None
            if rp.datas and rp.datas[0]["sha256"] != binding["sha256"]:
                if not accept_data_revision:
                    raise JournalError("these prints are not the prints of the first look (data digest differs): a different or reduced data set is a different test. "
                                       "Evaluate the same data, or explicitly accept a data revision (accept_data_revision), which is recorded in the chain")
                binding = dict(binding, revision=True)
            before = rp.ledger.n_trials
            rep = CP.evaluate(prints, hurdle_pct, rp.ledger, now_ms)        # the pure calculation, unchanged; it raises (and records nothing) when it refuses
            new = rp.ledger.trials[before]
            line_hash = _line_hash(rp.head or _LINE_GENESIS, new.entry_hash, binding)
            line = (json.dumps({"index": new.index, **{k: getattr(new, k) for k in _FIELDS}, "prev_hash": new.prev_hash, "entry_hash": new.entry_hash,
                                "data": binding, "line_hash": line_hash}, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
            created = not existed
            if created:
                fd = _open_regular(journal_path, os.O_RDWR | os.O_APPEND, create=True)
                if os.fstat(fd).st_size != 0:                               # a writer ignoring the lock created it first
                    raise JournalError("the journal appeared while it was held under the lock: refused (a writer ignored the lock)")
            elif os.fstat(fd).st_size != size:                              # compare-and-append: someone wrote without taking the lock
                raise JournalError("the journal changed while it was held under the lock: refused (a writer ignored the lock)")
            if not _same_inode(fd, journal_path):                           # the path was renamed/replaced since it was opened: an append would land on an orphan
                raise JournalError("the journal file was replaced or renamed while it was held under the lock: refused (a writer ignored the lock)")
            try:
                _write_all(fd, line)
                _os_fsync(fd)
                if created or size == 0:
                    dfd = os.open(os.path.dirname(os.path.abspath(journal_path)) or ".", os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        _os_fsync(dfd)
                    finally:
                        os.close(dfd)
            except OSError as exc:
                raise JournalWriteError(f"the append or its fsync failed ({exc.strerror or exc}); the look MAY OR MAY NOT be in the journal, which was not repaired. "
                                        "No result is reported for it. Copy the journal aside and reconcile it before any evaluation") from exc
            if not _same_inode(fd, journal_path):
                raise JournalWriteError("the journal file was replaced or renamed during the append: the look may be on an orphaned file. No result is reported for it")
        finally:
            if fd >= 0:
                os.close(fd)
    try:                                                             # the look is already journaled: nothing in this REPORT-ONLY step may lose its report or anchor
        sens = _selection_sensitivity(prints, hurdle_pct, raw, now_ms, rep, venue_floor_bps)
    except Exception as exc:                                         # noqa: BLE001  (deliberately broad: see above)
        sens = {"flipped_by_lowest_observed_fill": None, "breakeven_fill_bps": None, "breakeven_status": "NOT_EVALUATED", "note": f"the selection-sensitivity check could not be evaluated ({type(exc).__name__}): "
                                                                                "treat the verdict as NOT checked for selection"}
    n_after = before + 1
    rep = dict(rep)
    rep["selection_sensitivity"] = sens
    rep["journal"] = {"state_before": status, "n_trials": n_after, "head": line_hash, "record_this_anchor_outside_the_journal": {"n": n_after, "head": line_hash},
                      "data": binding, "checked_against_an_anchor": anchor is not None,
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
        p.add_argument("--anchor-file", default=None, help="file holding '<count> <head>' (refused if in the journal's directory; that check is NOT a trust boundary: the anchor must come from an independent, up-to-date record)")
        if name == "evaluate":
            p.add_argument("--funding", required=True, help="local .jsonl/.csv of settled prints: ts_ms, funding_bps")
            p.add_argument("--hurdle-pct", type=float, default=10.0)
            p.add_argument("--first-look", action="store_true", help="nothing has ever been recorded: create the journal")
            p.add_argument("--allow-unanchored", action="store_true", help="evaluate without an external anchor (the report states the limit)")
            p.add_argument("--venue-funding-floor-bps", type=float, default=None,
                           help="OWNER-SUPPLIED lowest funding a settlement can print (bps); the sensitivity check also fills missing settlements with it. Not verified here")
            p.add_argument("--accept-data-revision", action="store_true", help="the prints differ from the first look's: record that revision in the chain and evaluate")
    a = ap.parse_args(argv)
    try:
        anchor = _anchor_from_args(a)
        if a.cmd == "verify":
            rep = inspect(a.journal, anchor)
            print(json.dumps(rep, indent=2, sort_keys=True))
            return 0 if rep["ok_to_evaluate"] else 3
        rep = evaluate_logged(CP._load(a.funding), a.hurdle_pct, a.journal, anchor=anchor, first_look=a.first_look, allow_unanchored=a.allow_unanchored,
                              accept_data_revision=a.accept_data_revision, venue_floor_bps=a.venue_funding_floor_bps)
    except (JournalError, SP.StatisticalPowerError, AR.RecordError, OSError) as exc:
        print(f"trial_journal: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    j = rep["journal"]
    print(f"ANCHOR TO RECORD NOW, IN A RECORD HELD BY SOMEONE THE JOURNAL'S WRITER CANNOT INFLUENCE (off-host, append-only):  {j['n_trials']} {j['head']}\n"
          f"When you verify later, take the anchor with the HIGHEST count from that record: an older anchor still verifies a truncated journal.", file=sys.stderr)
    sens = rep["selection_sensitivity"]
    if sens.get("flipped_by_lowest_observed_fill"):
        print("\nWARNING: " + sens["note"], file=sys.stderr)
    elif sens.get("missing_settlements"):
        print(f"\nNOTE: {sens['missing_settlements']} settlement(s) are missing. {sens.get('note', '')}", file=sys.stderr)
    if j["data"]["revision"]:
        print("\nWARNING: this look used a DATA REVISION (the prints differ from the first look's); the revision is permanently recorded in the chain.", file=sys.stderr)
    try:
        print(json.dumps(rep, indent=2, sort_keys=True, allow_nan=False, default=str))
    except ValueError as exc:                                          # the look is journaled and its anchor is already printed above
        print(f"trial_journal: the report could not be serialised ({exc}); the look IS recorded, use the anchor printed above", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
