"""The files behind `seed_pilot`: charter, owner approval, intent log, flag.

`seed_pilot.decide` is pure; everything it must be GIVEN comes through here, and
every read fails closed. Nothing in this module places an order, imports a
broker, engine, store or config module, reads an environment variable or writes
anything except the pilot's own intent log. It is not imported by anything.

OWNER APPROVAL (the repository's existing convention, unchanged)
================================================================
The owner's act is recording the SHA-256 of the exact charter in an approvals
file OUTSIDE the repository (`promotion_gate`'s `_read_approvals`, the same
reader and the same checks the promotion gate uses: no symlink, regular file,
owned by the running account, no group/world write on the file or on a non-
sticky parent, read through one descriptor). Editing the charter afterwards
changes its digest and voids the approval. `tools/seed_pilot_approve.py` writes
it. This establishes that the approval sits in a file private to the running
account; like the promotion approvals it does NOT prove who wrote it - an actor
with that account's rights can write one. It is a barrier against a commit or
another account, not proof of human identity, and no stronger claim is made.

THE INTENT LOG
==============
An append-only file of hash-chained JSON lines (each line commits to the one
before it, so an edited, removed, reordered or truncated record is detected,
not repaired). It must exist before the pilot can run - a missing log is
UNAVAILABLE, never "empty" - and is created only by `IntentLog.initialise`.
Writes are flushed and fsynced before the call returns, which is what "durable
before any order" means: `seed_pilot.decide` ALLOWs a cycle only after this
module has returned from recording its INTENT.

Limitation, stated plainly: the hash chain detects accidental damage and edits
that do not recompute it; an actor with write access to the owner's account can
delete and recreate the log. The authoritative record of money is the
reconciled ledger (`carry_performance`), which the wiring must cross-check.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import promotion_gate as _pg
import seed_pilot as sp

GENESIS = "0" * 64
MAX_BYTES = 8 * 1024 * 1024
_EVENT_KEYS = set(sp.PilotEvent.__dataclass_fields__) | {"schema"}


class IntentLogError(RuntimeError):
    """The log cannot be believed or written; nothing was appended."""


# -- private-file reads -----------------------------------------------------------

def _private_problem(path: str, fd: int, real: str) -> Optional[str]:
    """Why the opened file may not be trusted, or None. Same rules as the
    promotion approvals reader."""
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        return f"{path!r} is not a regular file"
    if os.name == "posix":
        parent = os.path.dirname(real)
        pst = os.stat(parent)
        now = os.stat(real)
        if st.st_uid != os.geteuid():
            return f"{path!r} is owned by uid {st.st_uid}, not the running account"
        if st.st_mode & 0o022:
            return (f"{path!r} is group- or world-writable "
                    f"(mode {st.st_mode & 0o777:o})")
        if pst.st_mode & 0o022 and not pst.st_mode & stat.S_ISVTX:
            return (f"the parent directory of {path!r} is writable by others "
                    f"(mode {pst.st_mode & 0o777:o})")
        if (now.st_dev, now.st_ino) != (st.st_dev, st.st_ino):
            return f"{path!r} was replaced while being opened"
    return None


def _open_private(path: str, flags: int) -> Tuple[int, str]:
    """(fd, real path) of a private regular file, or raises IntentLogError. The
    final component is never followed."""
    if not isinstance(path, str) or not path.startswith("/") or "\x00" in path:
        raise IntentLogError(f"{path!r} is not an absolute path")
    real = os.path.join(os.path.realpath(os.path.dirname(path)),
                        os.path.basename(path))
    try:
        if stat.S_ISLNK(os.lstat(real).st_mode):
            raise IntentLogError(f"{path!r} is a symbolic link")
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise IntentLogError(f"{path!r} cannot be inspected ({exc.strerror})")
    try:
        fd = os.open(real, flags | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_CLOEXEC", 0))
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise IntentLogError(f"{path!r} cannot be opened safely ({exc.strerror})")
    why = None
    try:
        why = _private_problem(path, fd, real)
    except OSError as exc:
        why = f"{path!r} cannot be checked ({exc.strerror})"
    if why is not None:
        os.close(fd)
        raise IntentLogError(why)
    return fd, real


def _read_all(fd: int) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks, total = [], 0
    while True:
        b = os.read(fd, 65536)
        if not b:
            return b"".join(chunks)
        total += len(b)
        if total > MAX_BYTES:
            raise IntentLogError("file is larger than the limit")
        chunks.append(b)


# -- charter and approval -----------------------------------------------------------------

def load_charter(path: str) -> Tuple[Optional[sp.SeedPilotCharter], Tuple[str, ...]]:
    """(charter, problems). A charter file that is missing, not private, not
    JSON or not a well-formed charter yields (None, reasons)."""
    try:
        fd, _real = _open_private(path, os.O_RDONLY)
    except FileNotFoundError:
        return None, (f"{sp.CHARTER_MISSING}:{path}",)
    except IntentLogError as exc:
        return None, (f"{sp.CHARTER_INVALID}:{exc}",)
    try:
        payload = json.loads(_read_all(fd).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, IntentLogError) as exc:
        return None, (f"{sp.CHARTER_INVALID}:unreadable ({type(exc).__name__})",)
    finally:
        os.close(fd)
    charter, probs = sp.charter_from_dict(payload)
    return charter, tuple(probs)


def load_approval(charter: sp.SeedPilotCharter, approvals_path: str
                  ) -> sp.ApprovalStatus:
    """What the owner's approvals file says about THIS charter. An unusable
    file (inside the repo, a link, someone else's, writable by others) approves
    nothing and says why."""
    if not approvals_path:
        return sp.ApprovalStatus(None, ())
    try:
        approved, problem = _pg._read_approvals(str(approvals_path))
    except Exception as exc:  # noqa: BLE001 - fail closed
        return sp.ApprovalStatus(None, (f"approvals file unreadable "
                                        f"({type(exc).__name__})",))
    if problem is not None:
        return sp.ApprovalStatus(None, (problem,))
    return sp.ApprovalStatus(approved.get(sp.approval_key(charter.charter_id)), ())


# -- the manual termination flag ------------------------------------------------------------------

def read_termination_flag(path: str) -> Optional[bool]:
    """True if anything exists at `path` (the flag's content does not matter),
    False if it does not, None if that cannot be told - which refuses. A flag
    in a directory that does not exist could never be raised, so that is None."""
    if not isinstance(path, str) or not path.startswith("/"):
        return None
    try:
        os.lstat(path)
        return True
    except FileNotFoundError:
        return False if os.path.isdir(os.path.dirname(path)) else None
    except OSError:
        return None


# -- the intent log ----------------------------------------------------------------------------------

def _line_hash(prev: str, seq: int, event: Mapping[str, Any]) -> str:
    body = json.dumps(event, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)
    return hashlib.sha256(f"{prev}|{seq}|{body}".encode("utf-8")).hexdigest()


def _event_from_dict(d: Any) -> sp.PilotEvent:
    if not isinstance(d, Mapping) or set(d) != _EVENT_KEYS \
            or d.get("schema") != sp.EVENT_SCHEMA:
        raise IntentLogError("event has the wrong shape")
    try:
        return sp.PilotEvent(**{k: d[k] for k in sp.PilotEvent.__dataclass_fields__})
    except TypeError as exc:
        raise IntentLogError(f"event cannot be built ({exc})")


def _parse(raw: bytes) -> Tuple[List[sp.PilotEvent], str, int]:
    """(events, last hash, next seq) of a chained log, or raises."""
    if raw and not raw.endswith(b"\n"):
        raise IntentLogError("the log ends in a partial record")
    prev, seq, events = GENESIS, 0, []
    for number, line in enumerate(raw.decode("utf-8").splitlines(), 1):
        try:
            rec = json.loads(line)
        except ValueError:
            raise IntentLogError(f"line {number} is not JSON")
        if not isinstance(rec, dict) or set(rec) != {"seq", "prev", "event", "hash"}:
            raise IntentLogError(f"line {number} has the wrong shape")
        if rec["seq"] != seq or rec["prev"] != prev:
            raise IntentLogError(f"line {number} breaks the chain")
        try:
            want = _line_hash(prev, seq, rec["event"])
        except (TypeError, ValueError):
            raise IntentLogError(f"line {number} cannot be hashed")
        if rec["hash"] != want:
            raise IntentLogError(f"line {number} was altered")
        events.append(_event_from_dict(rec["event"]))
        prev, seq = rec["hash"], seq + 1
    return events, prev, seq


class IntentLog:
    """The pilot's durable record, bound to ONE charter. Every append is
    validated against the history it joins (`seed_pilot` folds it again), so a
    RESULT for an intent that was never recorded, a second RESULT, or an event
    under another charter is refused here, not discovered later."""

    def __init__(self, path: str, charter: sp.SeedPilotCharter) -> None:
        self.path = path
        self.charter = charter
        self.digest = sp.charter_digest(charter)

    # -- creation ------------------------------------------------------------
    def initialise(self) -> None:
        """Create the EMPTY log, once. Refuses an existing file, a path inside
        the repository, or a directory others can write to."""
        real = os.path.join(os.path.realpath(os.path.dirname(self.path)),
                            os.path.basename(self.path))
        _r, problem = _pg._approvals_location_problem(self.path)
        if problem is not None:
            raise IntentLogError(problem.replace("approvals file", "intent log"))
        pst = os.stat(os.path.dirname(real))
        if pst.st_mode & 0o022 and not pst.st_mode & stat.S_ISVTX:
            raise IntentLogError("the log's directory is writable by others")
        fd = os.open(real, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                     | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        dfd = os.open(os.path.dirname(real), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)

    # -- reading --------------------------------------------------------------
    def read(self) -> Tuple[Optional[Tuple[sp.PilotEvent, ...]], Optional[str]]:
        """(events, problem). Any trouble is (None, why): unavailable refuses."""
        _r, loc = _pg._approvals_location_problem(self.path)
        if loc is not None:
            return None, loc.replace("approvals file", "intent log")
        try:
            fd, _real = _open_private(self.path, os.O_RDONLY)
        except FileNotFoundError:
            return None, "the intent log does not exist"
        except IntentLogError as exc:
            return None, str(exc)
        try:
            events, _prev, _seq = _parse(_read_all(fd))
        except (IntentLogError, UnicodeDecodeError) as exc:
            return None, str(exc)
        finally:
            os.close(fd)
        folded, why = sp._fold(events, self.charter, self.digest)
        if folded is None:
            return None, "; ".join(why)
        return tuple(events), None

    # -- writing --------------------------------------------------------------
    def append(self, event: Mapping[str, Any]) -> sp.PilotEvent:
        """Validate, chain, write, fsync - or raise and write nothing."""
        ev = _event_from_dict(event)
        _r, loc = _pg._approvals_location_problem(self.path)
        if loc is not None:
            raise IntentLogError(loc.replace("approvals file", "intent log"))
        try:
            fd, _real = _open_private(self.path, os.O_RDWR | os.O_APPEND)
        except FileNotFoundError:
            raise IntentLogError("the intent log does not exist; initialise it first")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            events, prev, seq = _parse(_read_all(fd))
            folded, why = sp._fold(events + [ev], self.charter, self.digest)
            if folded is None:
                raise IntentLogError("event refused: " + "; ".join(why))
            body = ev.to_dict()
            line = json.dumps({"seq": seq, "prev": prev, "event": body,
                               "hash": _line_hash(prev, seq, body)},
                              sort_keys=True, separators=(",", ":"),
                              ensure_ascii=True, allow_nan=False) + "\n"
            data = line.encode("utf-8")
            if os.write(fd, data) != len(data):
                raise IntentLogError("short write; the log needs inspection")
            os.fsync(fd)
        finally:
            os.close(fd)                    # also releases the lock
        return ev

    def record_intent(self, decision: sp.PilotDecision) -> sp.PilotEvent:
        """Write the INTENT a decision asked for - and only that. A decision
        that did not ask for one (an ALLOW, or a REFUSE for any other reason)
        cannot be used to write a record."""
        if not isinstance(decision, sp.PilotDecision) \
                or not decision.intent_to_record or decision.verdict != sp.REFUSE:
            raise IntentLogError("this decision does not ask for an intent record")
        return self.append(decision.intent_to_record)

    def record_result(self, *, intent_id: str, outcome: str, net_usd: float,
                      at_s: float) -> sp.PilotEvent:
        return self.append(sp.PilotEvent(
            kind="RESULT", charter_id=self.charter.charter_id,
            charter_digest=self.digest, at_s=at_s, intent_id=intent_id,
            outcome=outcome, net_usd=net_usd).to_dict())

    def record_termination(self, *, reason: str, at_s: float) -> sp.PilotEvent:
        return self.append(sp.PilotEvent(
            kind="TERMINATED", charter_id=self.charter.charter_id,
            charter_digest=self.digest, at_s=at_s, reason=str(reason)).to_dict())


def build_state(charter: sp.SeedPilotCharter, *,
                book_open_cycle_ids: Optional[Sequence[str]],
                open_cycle_loss_usd: Optional[float],
                discovery_window_s: Optional[Tuple[float, float]],
                evidence_drawdown_limit_usd: Optional[float] = None
                ) -> sp.PilotState:
    """The state `decide` reads: the log and the flag from disk, the rest from
    the caller (the book and the owner's policy, which this module never
    reads)."""
    events, _problem = IntentLog(charter.intent_log_path, charter).read()
    return sp.PilotState(
        termination_flag_present=read_termination_flag(charter.termination_flag_path),
        events=events,
        book_open_cycle_ids=None if book_open_cycle_ids is None
        else tuple(str(i) for i in book_open_cycle_ids),
        open_cycle_loss_usd=open_cycle_loss_usd,
        discovery_window_s=discovery_window_s,
        evidence_drawdown_limit_usd=evidence_drawdown_limit_usd)
