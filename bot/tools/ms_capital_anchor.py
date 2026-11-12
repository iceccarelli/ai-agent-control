#!/usr/bin/env python3
"""ACCOUNT-BOUND, APPEND-ONLY CAPITAL LINEAGE: the capital book's integrity must not live in the file it can be deleted from.

THE DEFECT THIS CLOSES (Mission round 10, priority one). ``capital.json`` carried the loss streak, the drawdown peak and the lifetime loss stop, and its integrity was bound to its own file and directory.
Deleting it, restoring an older copy, pointing ``--root`` somewhere else, or running two roots on one account each produced a book that looked clean, and the documented way to lift the lifetime stop was
"move capital.json aside". A routine operational accident (a volume restore, a rebuilt state directory, a new ``--root``) therefore cleared every capital stop without a trace.

WHAT THE LINEAGE IS. One hash-chained JSONL file PER ACCOUNT, stored OUTSIDE ``--root`` (``<owner state>/capital_anchor/lineage-<account>.jsonl``, or ``$CAPITAL_ANCHOR_DIR``, which an owner may point at a second
volume). Every state-changing capital operation appends one entry (GENESIS, BASELINE, TRANSFER, PARK, UPDATE_PENDING, SETTLE, GAP, ACK_*), each carrying the book's complete stop-relevant state after the
operation, the rows it added to the history, and the digest of the whole resulting book. ``capital.json`` becomes a VIEW of the lineage head: it carries ``ledger_ref`` (account, seq, head hash, digest) and can
always be rebuilt from the lineage, so deleting or restoring it can never clear anything.

THE ACCOUNT IS THE VENUE'S, NOT THE FILE'S. The account reference is derived from the venue's own answer to ``GET /v5/user/query-api`` (``userID``) for the credentials in use, hashed with the venue key.
A state directory copied to another account, or used with other credentials, therefore carries another reference and is refused (ACCOUNT_MISMATCH); an unreadable identity is a refusal, never a guess.

THE COMMIT POINT is the fsynced append of the lineage entry; the view is written after it, so the lineage is never behind the view. A crash anywhere leaves the lineage ahead of (or equal to) the view and
the next open rebuilds the view forward from the attested head: a restored older view, a deleted view and a new root all converge to the recorded state, and the rebuild is printed and audited (never silent).

WHAT REFUSES (every refusal is a stable code, no refusal rewrites anything, nothing is repaired automatically):
  ACCOUNT_IDENTITY_UNAVAILABLE   the venue did not state the account (no userID): the book cannot be bound
  ACCOUNT_NOT_INITIALISED        no lineage exists for this account and no book: a NEW account needs the owner's act (``capital --anchor-init``); a wiped volume looks the same, which is why it is gated
  BOOK_UNANCHORED                a pre-anchor ``capital.json`` exists and no lineage: only the owner can adopt it as the account's history
  ANCHOR_MISSING_BOOK_BOUND      the book is bound to a lineage that is gone (anchor deleted or lost): the owner must decide, nothing is assumed
  ANCHOR_BEHIND_BOOK             the book is AHEAD of the lineage (an older anchor was restored)
  ANCHOR_CORRUPT                 the lineage does not verify (mutated, reordered, truncated in the middle, replay does not reproduce its digest); a torn FINAL line over a verifying chain is the one benign case
  VIEW_TAMPERED / VIEW_UNBOUND   the book does not match what the lineage attested at its own sequence, or it is a pre-anchor file where a lineage exists
  ACCOUNT_MISMATCH               the book or lineage belongs to another account or venue
  LOCAL_BEHIND_WITNESS / LINEAGE_FORKED / WITNESS_UNAVAILABLE   the optional remote witness (below)

THE REMOTE WITNESS (optional, owner-controlled). A witness holds the account's lineage head OUTSIDE this machine and refuses a second head at an existing sequence; with it a whole-volume rollback (which no
local copy can show) becomes LOCAL_BEHIND_WITNESS and a forked copy becomes LINEAGE_FORKED. The interface is here; the Supabase implementation needs a migration only the owner can apply, so until it is applied
the witness is NOT_CONFIGURED and every report says so. Nothing in this module reaches the network by itself.

HONEST LIMITS (the threat boundary). (1) An operator who can replace EVERY local file (the lineage, its lock, the view) and has no witness to answer to can still forge a consistent history; the lineage makes
that deliberate and visible, not impossible. (2) With no witness, a consistent rollback of the whole volume restores an older lineage; the next settlement's venue cash read re-books the cash that went
missing (the book's GAP entry), which is why cash continuity is checked, but a rollback that moved no cash is not visible. (3) The owner can abandon a lineage by moving the file aside by hand and running
``capital --anchor-init``; that is a deliberate act of a privileged operator, and it is refused when a witness knows the account. (4) The identity is only as strong as the venue's ``userID``.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sys
import time
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import ms_engine_state as ES  # noqa: E402

LINEAGE_SCHEMA = "capital_lineage.v1"
VIEW_NAME = "capital.json"
GENESIS_HASH = "00" * 32
HISTORY_MAX = 200                      # the book's history / applied-transfer window (ms_capital.HISTORY_MAX is this value)
ANCHOR_ENV = "CAPITAL_ANCHOR_DIR"
WITNESS_ENV = "CAPITAL_WITNESS"        # "" / "off" = not configured; "supabase" = the Supabase RPC witness (needs the owner-applied migration)
WITNESS_REQUIRED_ENV = "CAPITAL_WITNESS_REQUIRED"
KINDS = frozenset({"GENESIS", "BASELINE", "RESERVE", "TRANSFER", "PARK", "UPDATE_PENDING", "SETTLE", "GAP", "ACK_HALT", "ACK_REVIEW", "ACK_DRAWDOWN", "REBASELINE"})
_ACCOUNT_RE = re.compile(r"^acct_[0-9a-f]{24}$")
_UID_RE = re.compile(r"^[0-9]{1,20}$")
_BIG = ("history", "transfers_applied")           # the two growing lists: rebuilt from the entries' deltas, never copied whole into every entry


LOCK_TIMEOUT_S = 30.0                 # a holder that does not let go in this long is stuck (a witness call in flight is bounded well below it): the caller refuses, it never hangs forever


def flock_with_timeout(fd: int, timeout_s: float, what: str) -> None:
    """``flock(LOCK_EX)`` that gives up: LOCK_TIMEOUT (transient) instead of blocking an owner command or the loop behind a stuck process."""
    import errno
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if exc.errno not in (errno.EAGAIN, errno.EACCES):
                raise
            if time.monotonic() >= deadline:
                raise AnchorRefused("LOCK_TIMEOUT", f"{what} is held by another process for more than {timeout_s:.0f}s") from None
            time.sleep(0.02)


class AnchorRefused(Exception):
    """The capital book may not be used. ``code`` is stable (a test, an alert and a runbook key on it); ``detail`` is for the owner and never contains a secret."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


class WitnessError(Exception):
    """The witness could not answer (unreachable, malformed answer, refused). It never means 'no history'."""


class WitnessConflict(WitnessError):
    """The witness already holds a DIFFERENT head at this sequence: two histories for one account."""


class NoWitness:
    """No remote witness is configured: the report says so; nothing is published and nothing is checked remotely."""
    configured = False
    required = False
    name = "NOT_CONFIGURED"

    def head(self, account: str) -> Optional[Tuple[int, str]]:                        # pragma: no cover - never called when not configured
        raise WitnessError("NOT_CONFIGURED")

    def publish(self, account: str, entry: Mapping[str, Any]) -> None:               # pragma: no cover
        raise WitnessError("NOT_CONFIGURED")


# ------------------------------------------------------------------------------------------------ identity
def account_ref_of(venue_key: Any, user_id: Any) -> Optional[str]:
    """``acct_`` + 24 hex of sha256(venue | userID). None when the venue did not state a usable account (a bool, an empty value or anything but digits): the caller must refuse, never guess. Only the hash is
    ever stored or printed."""
    if isinstance(user_id, bool) or user_id is None:
        return None
    uid = str(user_id).strip()
    vk = str(venue_key or "").strip()
    if not vk or not _UID_RE.match(uid):
        return None
    return "acct_" + hashlib.sha256(f"capital.account.v1|{vk}|{uid}".encode("utf-8")).hexdigest()[:24]


def account_ref_of_key(venue_key: Any, key_id: Any) -> Optional[str]:
    """The FALLBACK identity, used only when the owner sets ``CAPITAL_ACCOUNT_IDENTITY_FALLBACK=KEY_ID`` because the venue's key read states no ``userID``: a hash of the API key's public id. Weaker than the userID
    (rotating the key is then a new account: the next session refuses with ACCOUNT_NOT_INITIALISED / ACCOUNT_MISMATCH until the owner acts), so it is never chosen silently, and a different domain keeps the two
    identities from ever colliding. The raw id is hashed at once and never stored or printed."""
    if isinstance(key_id, bool) or key_id is None:
        return None
    kid, vk = str(key_id).strip(), str(venue_key or "").strip()
    if not vk or not (1 <= len(kid) <= 64) or not re.fullmatch(r"[A-Za-z0-9_-]+", kid):
        return None
    return "acct_" + hashlib.sha256(f"capital.account.v1.keyid|{vk}|{kid}".encode("utf-8")).hexdigest()[:24]


def is_account_ref(x: Any) -> bool:
    return isinstance(x, str) and bool(_ACCOUNT_RE.match(x))


def anchor_dir_for(env: Optional[Mapping[str, str]] = None, *, default_home_file: Optional[str] = None) -> str:
    """``$CAPITAL_ANCHOR_DIR`` (an owner may mount a second volume there) or ``capital_anchor`` next to the owner's capability file (``~/.ai-agent-control``): ONE place whatever ``--root`` says."""
    e = os.environ if env is None else env
    p = (e.get(ANCHOR_ENV) or os.environ.get(ANCHOR_ENV) or "").strip()      # an env mapping handed to a command (a test's {}) must not hide the process's own setting
    if p:
        return os.path.abspath(os.path.expanduser(p))
    base = os.path.dirname(default_home_file) if default_home_file else os.path.join(os.path.expanduser("~"), ".ai-agent-control")
    return os.path.join(base, "capital_anchor")


def witness_from_env(env: Mapping[str, str]) -> Any:
    """The configured remote witness, or ``NoWitness``. ``CAPITAL_WITNESS=supabase`` selects the Supabase RPC witness (its migration is the OWNER's to apply: ``docs/MONEY_MISSION.md``); ``CAPITAL_WITNESS_REQUIRED=1``
    makes an unreachable witness a refusal. A witness that was asked for and cannot be built is a refusal, never a silent NoWitness."""
    mode = (env.get(WITNESS_ENV) or "").strip().lower()
    required = (env.get(WITNESS_REQUIRED_ENV) or "").strip().lower() in ("1", "true", "yes")
    if mode in ("", "off", "none"):
        if required:
            raise AnchorRefused("WITNESS_MISCONFIGURED", f"{WITNESS_REQUIRED_ENV} is set but no witness is configured ({WITNESS_ENV})")
        return NoWitness()
    if mode == "supabase":
        try:
            import ms_capital_witness as W                                    # noqa: WPS433 - lazy: only an owner who asked for the witness needs the Supabase client
            return W.SupabaseWitness.from_env(env, required=required)
        except AnchorRefused:
            raise
        except Exception as exc:                                              # noqa: BLE001
            raise AnchorRefused("WITNESS_MISCONFIGURED", f"the Supabase witness could not be built ({type(exc).__name__})") from None
    raise AnchorRefused("WITNESS_MISCONFIGURED", f"unknown {WITNESS_ENV} value {mode!r}")


# ------------------------------------------------------------------------------------------------ chain primitives
def _canon(o: Any) -> str:
    return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def entry_hash(prev: str, body: Mapping[str, Any]) -> str:
    return hashlib.sha256((prev + _canon({k: v for k, v in body.items() if k != "h"})).encode("utf-8")).hexdigest()


def book_digest(book: Mapping[str, Any]) -> str:
    """Digest of the WHOLE book (state, history, applied transfers) without its own ``ledger_ref``: what a view must equal at the sequence it claims."""
    return hashlib.sha256(_canon({k: v for k, v in book.items() if k != "ledger_ref"}).encode("utf-8")).hexdigest()


def compact_state(book: Mapping[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in book.items() if k not in _BIG and k != "ledger_ref"}


def rebuild(entries: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """The book at the head of ``entries``: the head's state plus the last HISTORY_MAX history rows and applied transfers accumulated over every entry."""
    if not entries:
        raise AnchorRefused("ANCHOR_CORRUPT", "empty lineage")
    hist: List[Any] = []
    tr: List[Any] = []
    for e in entries:
        hist.extend(e.get("history_add") or [])
        tr.extend(e.get("transfers_applied_add") or [])
    book = dict(entries[-1]["state"])
    book["history"] = hist[-HISTORY_MAX:]
    book["transfers_applied"] = tr[-HISTORY_MAX:]
    return book


def _fsync_dir(path: str) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


class Lineage:
    """One account's append-only file. Reading verifies the whole chain; a torn FINAL line over a verifying chain is set aside as evidence (``.torn``) and dropped (the append never completed, so it was never
    acted on); anything else wrong is ANCHOR_CORRUPT and the file is left exactly as it is."""

    def __init__(self, path: str, account: str, venue: str) -> None:
        self.path, self.account, self.venue = path, account, venue

    def exists(self) -> bool:
        return os.path.lexists(self.path) and os.path.getsize(self.path) > 0

    def read(self, *, repair_torn: bool = True) -> List[Dict[str, Any]]:
        try:
            with open(self.path, "rb") as fh:
                data = fh.read()
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise AnchorRefused("ANCHOR_CORRUPT", f"the lineage cannot be read ({type(exc).__name__})") from None
        parts = data.split(b"\n")
        tail = parts.pop()                                              # b"" when the file ends with a newline
        raws = [p for p in parts if p.strip()]
        torn: Optional[bytes] = None
        if tail.strip():
            raws.append(tail)
        out: List[Dict[str, Any]] = []
        prev = GENESIS_HASH
        for i, raw in enumerate(raws):
            try:
                e = json.loads(raw)
                if not isinstance(e, dict):
                    raise ValueError("not an object")
            except (ValueError, RecursionError):
                if i == len(raws) - 1 and raw is tail:
                    torn = tail                                          # the unparseable FINAL, newline-less line: a torn append
                    break
                raise AnchorRefused("ANCHOR_CORRUPT", f"line {i} is not valid JSON") from None
            why = self._check(e, i, prev)
            if why is not None:
                raise AnchorRefused("ACCOUNT_MISMATCH" if why.startswith("ACCOUNT") else "ANCHOR_CORRUPT", why)
            out.append(e)
            prev = e["h"]
        if torn is not None and repair_torn:
            self._set_aside(torn, len(data) - len(torn))
        return out

    def _check(self, e: Mapping[str, Any], i: int, prev: str) -> Optional[str]:
        if e.get("schema") != LINEAGE_SCHEMA:
            return f"line {i} has another schema"
        if e.get("seq") != i or isinstance(e.get("seq"), bool):
            return f"SEQ_GAP_AT_{i}"
        if e.get("prev") != prev:
            return f"CHAIN_BROKEN_AT_{i}"
        h = e.get("h")
        if not isinstance(h, str) or entry_hash(prev, e) != h:
            return f"MUTATED_AT_{i}"
        if e.get("account") != self.account or e.get("venue") != self.venue:
            return f"ACCOUNT_OR_VENUE_MISMATCH_AT_{i}"
        if e.get("kind") not in KINDS or not isinstance(e.get("state"), dict) or not isinstance(e.get("book_digest"), str):
            return f"MALFORMED_AT_{i}"
        for k in ("history_add", "transfers_applied_add"):
            if not isinstance(e.get(k), list):
                return f"MALFORMED_AT_{i}"
        return None

    def _set_aside(self, text: bytes, at: int) -> None:
        """The torn tail is evidence: keep its bytes, then cut the file back to the last complete line. Printed, never silent."""
        try:
            with open(self.path + ".torn", "ab") as fh:
                fh.write(text + b"\n")
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:
            print(f"CAPITAL_LINEAGE_TORN_TEXT_NOT_KEPT: {type(exc).__name__}", file=sys.stderr)
        with open(self.path, "r+b") as fh:
            fh.truncate(at)
            fh.flush()
            os.fsync(fh.fileno())
        print(f"CAPITAL_LINEAGE_TORN_TAIL_DROPPED: {len(text)} bytes of an unfinished append were set aside in {os.path.basename(self.path)}.torn; every earlier entry verified", file=sys.stderr)

    def append(self, entry: Mapping[str, Any]) -> None:
        """Durable append: the line (and a newline first when the file ends without one), fsynced; the directory is fsynced when the file is created."""
        line = (_canon(entry) + "\n").encode("utf-8")
        created = not os.path.lexists(self.path)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            if not created and os.path.getsize(self.path) > 0:
                with open(self.path, "rb") as rh:
                    rh.seek(-1, os.SEEK_END)
                    if rh.read(1) != b"\n":
                        line = b"\n" + line
            n = os.write(fd, line)
            if n != len(line):
                raise OSError("short write to the capital lineage")
            os.fsync(fd)
        finally:
            os.close(fd)
        if created:
            _fsync_dir(os.path.dirname(self.path))


# ------------------------------------------------------------------------------------------------ the anchor (what a book talks to)
def _noop(_name: str) -> None:
    return None


#: A seam the crash tests replace: called with "before_append", "after_append", "after_view" and "after_witness". Production leaves it a no-op.
crash_hook: Callable[[str], None] = _noop


class Anchor:
    def __init__(self, root: str, account: str, venue: str, anchor_dir: str, *, witness: Any = None, clock: Optional[Callable[[], int]] = None, verify_only: bool = False) -> None:
        if not is_account_ref(account):
            raise AnchorRefused("ACCOUNT_IDENTITY_UNAVAILABLE", "the account reference is not a valid acct_ hash")
        self.root, self.account, self.venue, self.anchor_dir = root, account, venue, anchor_dir
        self.clock = clock or (lambda: int(time.time() * 1000))
        self.witness = witness if witness is not None else NoWitness()
        self.verify_only = verify_only
        self.view_path = os.path.join(root, VIEW_NAME)
        self.lineage = Lineage(os.path.join(anchor_dir, f"lineage-{account}.jsonl"), account, venue)
        self._lock_path = os.path.join(anchor_dir, f"lineage-{account}.lock")
        self._entries: Optional[List[Dict[str, Any]]] = None     # the verified entries inside one locked scope
        self._seen: Tuple[int, Optional[str]] = (0, None)       # (entries, head hash) as of the last read or append: what report() states after the lock is released
        self.rebuilt: Optional[str] = None                      # why this instance had to rebuild the view (sticky: the first thing a report must say; None = it never needed to)
        self.witness_state = "NOT_CONFIGURED" if not getattr(self.witness, "configured", False) else "UNCHECKED"

    # ---- locking: the anchor lock is the OUTER lock (every root that shares an account serialises here)
    @contextmanager
    def locked(self) -> Iterator[None]:
        if self.verify_only and not os.path.lexists(self._lock_path):
            self._entries = None                                     # a read-only open never creates the directory or the lock; with no lock file no writer has ever used it
            try:
                yield
            finally:
                self._entries = None
            return
        os.makedirs(self.anchor_dir, mode=0o700, exist_ok=True)
        fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            flock_with_timeout(fd, LOCK_TIMEOUT_S, f"the capital lineage lock of {self.account}")
            self._entries = None
            try:
                yield
            finally:
                self._entries = None
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    # ---- audit: every rebuild, adoption and genesis is printed AND appended (fsynced) to ``capital_anchor_log.jsonl`` in the root; the owner's own acts are on ``marker_log.jsonl`` (the gate).
    # Best effort: a failure to audit never blocks a refusal, and the lineage itself is the durable record of every state change.
    def audit(self, action: str, payload: Mapping[str, Any]) -> None:
        row = {"anchor_event": action, "at_ms": int(self.clock()), "account": self.account, **dict(payload)}
        print(f"CAPITAL_ANCHOR_{action}: " + json.dumps({k: v for k, v in row.items() if k != "at_ms"}, sort_keys=True, default=str), file=sys.stderr)
        try:
            os.makedirs(self.root, mode=0o700, exist_ok=True)
            fd = os.open(os.path.join(self.root, "capital_anchor_log.jsonl"), os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            try:
                os.write(fd, (json.dumps(row, sort_keys=True, default=str) + "\n").encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass

    # ---- reading
    def _read_view(self) -> Tuple[str, Optional[Dict[str, Any]]]:
        """('absent'|'unreadable'|'legacy'|'bound', book). 'legacy' parses as a book but carries no ledger_ref."""
        try:
            with open(self.view_path, encoding="utf-8") as fh:
                d = json.load(fh)
        except FileNotFoundError:
            return "absent", None
        except (OSError, ValueError, RecursionError):
            return "unreadable", None
        if not isinstance(d, dict):
            return "unreadable", None
        if not isinstance(d.get("ledger_ref"), dict):
            return "legacy", d
        return "bound", d

    def entries(self) -> List[Dict[str, Any]]:
        if self._entries is None:
            self._entries = self.lineage.read(repair_torn=not self.verify_only)
            self._seen = (len(self._entries), self._entries[-1]["h"] if self._entries else None)
        return self._entries

    def head(self) -> Optional[Dict[str, Any]]:
        e = self.entries()
        return e[-1] if e else None

    # ---- the open: the case table that decides whether the book may be used
    def open_view(self) -> Dict[str, Any]:
        """The verified book (a dict with ``ledger_ref``), rebuilt from the lineage when the view is missing or older. MUST be called inside ``locked()``. Raises ``AnchorRefused``; nothing is rewritten on a refusal."""
        entries = self.entries()
        status, view = self._read_view()
        if status == "bound":
            bound_to = (view.get("ledger_ref") or {}).get("account")            # type: ignore[union-attr]
            if bound_to != self.account or view.get("account") not in (None, self.account):   # type: ignore[union-attr]
                raise AnchorRefused("ACCOUNT_MISMATCH", f"the book is bound to account {bound_to}, not {self.account}: a state directory belongs to the account it was created for")
        if not entries:
            if status == "bound":
                ref = view.get("ledger_ref") or {}               # type: ignore[union-attr]
                raise AnchorRefused("ANCHOR_MISSING_BOOK_BOUND", f"the book names lineage {ref.get('account')} seq {ref.get('seq')} head {str(ref.get('head'))[:12]}, and no lineage exists at {self.lineage.path}")
            self._propose(adopt=status in ("legacy", "unreadable"))
            if status in ("legacy", "unreadable"):
                raise AnchorRefused("BOOK_UNANCHORED", "a capital book exists with no lineage: only the owner can adopt it (capital --anchor-init)")
            raise AnchorRefused("ACCOUNT_NOT_INITIALISED", "no lineage exists for this account: a new account needs the owner's act (capital --anchor-init)")
        head = entries[-1]
        replay = rebuild(entries)
        if book_digest(replay) != head["book_digest"]:
            raise AnchorRefused("ANCHOR_CORRUPT", "the lineage does not reproduce the book digest it attests at its head")
        if status == "absent":
            return self._heal(replay, head, "NO_VIEW", None)
        if status == "unreadable":
            self._preserve("unreadable")
            return self._heal(replay, head, "VIEW_UNREADABLE", None)
        if status == "legacy":
            raise AnchorRefused("VIEW_UNBOUND", "capital.json is a pre-anchor file but this account has a lineage; the owner must decide (capital --rebuild-view)")
        ref = view["ledger_ref"]                                  # type: ignore[index]
        vs = ref.get("seq")
        if isinstance(vs, bool) or not isinstance(vs, int) or vs < 0:
            raise AnchorRefused("VIEW_TAMPERED", "the book's ledger_ref is malformed")
        if vs > head["seq"]:
            raise AnchorRefused("ANCHOR_BEHIND_BOOK", f"the book is at seq {vs} and the lineage ends at seq {head['seq']}: an older anchor was restored, or entries were removed")
        at = entries[vs]
        if ref.get("head") != at["h"] or ref.get("digest") != at["book_digest"] or book_digest(view) != at["book_digest"]:     # type: ignore[arg-type]
            raise AnchorRefused("VIEW_TAMPERED", f"the book does not match what the lineage attested at seq {vs}")
        if vs == head["seq"]:
            return dict(view)                                       # type: ignore[arg-type]
        # an older view of THIS lineage: a crash between the append and the view write (one entry behind) or a restored backup. The lineage is the truth; forward only, loudly.
        if vs < head["seq"] - 1:
            self._preserve("stale")
        return self._heal(replay, head, "CRASH_WINDOW" if vs == head["seq"] - 1 else f"STALE_VIEW_SEQ_{vs}", vs)

    def _heal(self, replay: Dict[str, Any], head: Mapping[str, Any], why: str, from_seq: Optional[int]) -> Dict[str, Any]:
        view = {**replay, "ledger_ref": {"account": self.account, "seq": head["seq"], "head": head["h"], "digest": head["book_digest"]}}
        self.rebuilt = why
        if self.verify_only:
            return view                                           # a read-only open reports what the view WOULD be and writes nothing
        ES.write_json_atomic(self.view_path, view)
        self.audit("VIEW_REBUILT", {"why": why, "from_seq": from_seq, "to_seq": head["seq"], "head": head["h"][:16]})
        return dict(view)

    def _preserve(self, tag: str) -> None:
        """Keep an unreadable or stale view as evidence before it is replaced."""
        if self.verify_only or not os.path.lexists(self.view_path):
            return
        dst = f"{self.view_path}.{tag}.{int(self.clock())}"
        try:
            with open(self.view_path, "rb") as src, open(dst, "xb") as out:
                out.write(src.read())
                out.flush()
                os.fsync(out.fileno())
        except OSError as exc:
            print(f"CAPITAL_VIEW_EVIDENCE_NOT_KEPT: {type(exc).__name__}", file=sys.stderr)

    def _propose(self, adopt: bool) -> None:
        """Leave the owner what ``capital --anchor-init`` needs: the account reference this session saw. Convenience only: the next session re-verifies the account against the venue, so a wrong proposal can only be refused."""
        if self.verify_only:
            return
        try:
            ES.write_json_atomic(os.path.join(self.anchor_dir, f"proposal-{self.account}.json"),
                                 {"schema": "capital_anchor_proposal.v1", "account": self.account, "venue": self.venue, "seen_ms": int(self.clock()), "legacy_book_present": bool(adopt)})
        except OSError:
            pass

    # ---- the one write path
    def commit(self, book: Mapping[str, Any], kind: str, facts: Optional[Mapping[str, Any]] = None, history_add: Sequence[Any] = (), transfers_add: Sequence[Any] = ()) -> Dict[str, Any]:
        """Append the entry (the commit point), then write the view, then tell the witness. Inside ``locked()``, after ``open_view``."""
        if self.verify_only:
            raise AnchorRefused("READ_ONLY", "this anchor was opened read-only")
        if kind not in KINDS:
            raise ValueError(f"unknown capital lineage kind {kind!r}")
        entries = self.entries()
        seq = len(entries)
        prev = entries[-1]["h"] if entries else GENESIS_HASH
        b = {k: v for k, v in book.items() if k != "ledger_ref"}
        b["account"], b["venue"] = self.account, self.venue
        entry: Dict[str, Any] = {"schema": LINEAGE_SCHEMA, "seq": seq, "prev": prev, "account": self.account, "venue": self.venue, "kind": kind, "ts_ms": int(self.clock()),
                                 "state": compact_state(b), "history_add": list(history_add), "transfers_applied_add": list(transfers_add), "facts": dict(facts or {}), "book_digest": book_digest(b)}
        entry["h"] = entry_hash(prev, entry)
        # the whole resulting book must equal what a replay of the entries reproduces (else the caller's history/transfer deltas are wrong and a rebuild would DIFFER from the view)
        if book_digest(rebuild(list(entries) + [entry])) != entry["book_digest"]:
            raise AnchorRefused("ANCHOR_CORRUPT", f"{kind}: the operation's history deltas do not reproduce the book it saves (refusing to write an entry a rebuild could not reproduce)")
        crash_hook("before_append")
        self.lineage.append(entry)
        entries.append(entry)
        self._seen = (len(entries), entry["h"])
        crash_hook("after_append")
        ES.write_json_atomic(self.view_path, {**b, "ledger_ref": {"account": self.account, "seq": seq, "head": entry["h"], "digest": entry["book_digest"]}})
        crash_hook("after_view")
        self._publish(entries)
        crash_hook("after_witness")
        return entry

    # ---- the remote witness
    def _ref(self, h: str) -> str:
        f = getattr(self.witness, "ref_of", None)
        return f(h) if callable(f) else h

    def check_witness(self, *, publish: bool = True) -> str:
        """Compare the local head with the witness and publish the head when the witness is behind. LOCAL_BEHIND_WITNESS (the local lineage is OLDER than what the witness holds: a rollback or a replaced volume),
        LINEAGE_FORKED (another history at a sequence both hold) and, when the witness is REQUIRED, WITNESS_UNAVAILABLE (unreachable, or the current head could not be acknowledged) refuse. An optional witness
        that cannot answer is recorded in ``witness_state`` and the session goes on: say so in every report."""
        w = self.witness
        if not getattr(w, "configured", False):
            self.witness_state = "NOT_CONFIGURED"
            return self.witness_state
        required = bool(getattr(w, "required", False))
        entries = self.entries()
        try:
            rh = w.head(self.account)
        except WitnessError as exc:
            self.witness_state = "UNAVAILABLE"
            if required:
                raise AnchorRefused("WITNESS_UNAVAILABLE", f"the remote witness could not be read ({str(exc)[:100]}) and it is required") from None
            return self.witness_state
        if rh is not None:
            rseq, rref = rh
            if rseq > len(entries) - 1:
                raise AnchorRefused("LOCAL_BEHIND_WITNESS", f"the witness holds seq {rseq} and the local lineage ends at seq {len(entries) - 1}: the local state was rolled back or replaced")
            if self._ref(entries[rseq]["h"]) != rref:
                raise AnchorRefused("LINEAGE_FORKED", f"the witness holds a different head at seq {rseq} than the local lineage")
        if publish and entries and (rh is None or rh[0] < len(entries) - 1):
            try:
                w.publish(self.account, entries[-1])                      # the HEAD only: the witness needs the position, not the whole history
            except WitnessConflict:
                raise AnchorRefused("LINEAGE_FORKED", f"the witness holds a different head at seq {len(entries) - 1} than the local lineage") from None
            except WitnessError as exc:
                self.witness_state = "UNAVAILABLE"
                if required:
                    raise AnchorRefused("WITNESS_UNAVAILABLE", f"the current head could not be acknowledged by the required witness ({str(exc)[:100]})") from None
                return self.witness_state
        self.witness_state = "OK" if (publish or rh is not None) else "REMOTE_EMPTY"
        return self.witness_state

    def _publish(self, entries: Sequence[Mapping[str, Any]]) -> None:
        """Best effort after a commit (the lineage is the durable record). The HEAD is what the witness needs; a failure is recorded, and the barrier before the first order (``check_witness``) makes a REQUIRED witness
        a precondition of trading, so an outage can delay a session but never lets one proceed unwitnessed."""
        w = self.witness
        if not getattr(w, "configured", False) or not entries:
            return
        try:
            w.publish(self.account, entries[-1])
            self.witness_state = "OK"
        except WitnessConflict:
            self.witness_state = "FORKED"
            self.audit("WITNESS_FORK", {"local_seq": len(entries) - 1})
        except WitnessError:
            self.witness_state = "UNAVAILABLE"

    # ---- the one place the owner's act creates a lineage
    def initialise(self, *, adopt: Optional[Mapping[str, Any]], fresh_book: Mapping[str, Any], owner: Mapping[str, Any]) -> Dict[str, Any]:
        """GENESIS. Inside ``locked()``. Refused when a lineage exists or when a configured witness knows the account (or cannot be read). ``adopt`` is a validated pre-anchor book (kept whole, with its digest); the
        owner's gate facts are recorded in the entry."""
        if self.entries():
            raise AnchorRefused("GENESIS_REFUSED_LINEAGE_EXISTS", "this account already has a lineage; it can only be extended, never re-created")
        w = self.witness
        if getattr(w, "configured", False):
            try:
                rh = w.head(self.account)
            except WitnessError as exc:
                raise AnchorRefused("WITNESS_UNAVAILABLE", f"the remote witness could not be read ({str(exc)[:80]}); a new lineage cannot be created blind") from None
            if rh is not None:
                raise AnchorRefused("GENESIS_REFUSED_WITNESS_HAS_HISTORY", f"the remote witness already holds seq {rh[0]} for this account: the account is not new")
        book = dict(adopt) if adopt is not None else dict(fresh_book)
        hist = list(book.get("history") or [])
        tr = list(book.get("transfers_applied") or [])
        facts = {"owner": dict(owner), "adopted": adopt is not None, "adopted_digest": book_digest(adopt) if adopt is not None else None,
                 "witness": "NOT_CONFIGURED" if not getattr(w, "configured", False) else "CONFIRMED_NO_HISTORY"}
        entry = self.commit(book, "GENESIS", facts, hist, tr)
        self.audit("GENESIS", {"adopted": adopt is not None, "seq": entry["seq"], "head": entry["h"][:16], "witness": facts["witness"]})
        return entry

    def report(self) -> Dict[str, Any]:
        n, head = self._seen
        return {"anchored": True, "account": self.account, "venue": self.venue, "lineage_file": os.path.basename(self.lineage.path), "lineage_entries": n, "head_seq": n - 1 if n else None,
                "head": head[:16] if head else None, "witness": self.witness_state, "view_rebuilt": self.rebuilt}


# ------------------------------------------------------------------------------------------------ helpers for the CLI (no venue access)
def lineage_accounts(anchor_dir: str) -> List[str]:
    try:
        names = os.listdir(anchor_dir)
    except OSError:
        return []
    return sorted(m.group(1) for m in (re.match(r"^lineage-(acct_[0-9a-f]{24})\.jsonl$", n) for n in names) if m)


def proposals(anchor_dir: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    try:
        names = sorted(os.listdir(anchor_dir))
    except OSError:
        return out
    for n in names:
        if re.match(r"^proposal-acct_[0-9a-f]{24}\.json$", n):
            d = ES.read_json(os.path.join(anchor_dir, n))
            if isinstance(d, dict) and is_account_ref(d.get("account")):
                out.append(d)
    return out


def resolve_account(root: str, anchor_dir: str) -> Optional[str]:
    """The account an offline command (the owner's ``capital`` CLI, the read-only decision) should act on: the one the root's book is bound to, else the only lineage in the anchor directory. Ambiguity is
    None (the caller refuses); the next session re-verifies it against the venue either way."""
    d = ES.read_json(os.path.join(root, VIEW_NAME))
    if isinstance(d, dict):
        ref = d.get("ledger_ref")
        if isinstance(ref, dict) and is_account_ref(ref.get("account")):
            return str(ref["account"])
    accts = lineage_accounts(anchor_dir)
    return accts[0] if len(accts) == 1 else None
