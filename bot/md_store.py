"""Durable, append-only, hash-chained store of every market-data input.

One ordered log. Every input that can change what the market state believes is a
record in it, in arrival order: raw WebSocket frames, connection events, REST
bodies. Because the live path and the replay path both fold the SAME records
through the SAME function (`md_engine.apply`), a replay of a stored session
reproduces the state, and every stored view, exactly. That is the determinism
guarantee, and it holds by construction rather than by a parallel
re-implementation.

* **Separate from the trading runtime database.** The store is a directory of
  SQLite segment files (one per UTC day) chosen by the caller. It refuses a path
  inside a tracked source tree and refuses to open ``trading_state.db``.
* **Append-only.** SQL triggers abort UPDATE and DELETE on the log.
* **Tamper-evident.** Each record carries ``chain = sha256(prev_chain | record
  fields | sha256(payload))``; :meth:`MarketStore.verify` recomputes the chain.
  The chain is unkeyed, so it proves internal consistency, not authenticity.
* **Provenance first.** The payload is the exact text the venue sent (zlib
  compressed), with the local wall and monotonic receive times.

Durability: records are committed in small batches (``commit_every`` records or
``commit_interval_s``). A crash can lose the last fraction of a second of
*market data*; it cannot corrupt the log, and it holds no trading state.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import threading
import time
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Iterator, List, Optional, Tuple

FRAME = "frame"        # a raw WebSocket text frame
CONN = "conn"          # a connection event: {"event": connected|disconnected|invalidate, ...}
REST = "rest"          # a REST exchange: {"endpoint","params","http_status","body"}
VIEW = "view"          # a derived MarketView, logged for audit and replay checks
KINDS = (FRAME, CONN, REST, VIEW)

SCHEMA_VERSION = 1
GENESIS = "0" * 64

#: Preset zlib dictionary: representative public-feed frames. Small frames
#: (a book delta is ~150 bytes) compress more than twice as well with it. It is
#: part of the on-disk format: changing it requires a new DICT_VERSION.
DICT_VERSION = 1
_ZDICT = (
    b'{"topic":"orderbook.50.BTCUSDT","type":"delta","ts":1793819857554,"data":{"s":"BTCUSDT",'
    b'"b":[["80257.20","0.053"],["80256.80","0"]],"a":[["80258.00","1.250"],["80258.10","0"]],'
    b'"u":67964858,"seq":838875713545},"cts":1793819857553}'
    b'{"topic":"orderbook.50.BTCUSDT","type":"snapshot","ts":1793819857495,"data":{"s":"BTCUSDT","b":[["80268.50","2.797"],'
    b'["80268.40","0.001"]],"a":[["80268.60","4.984"],["80268.70","0.100"]],"u":67964857,"seq":838875713000},"cts":1793819857494}'
    b'{"topic":"tickers.BTCUSDT","type":"delta","data":{"symbol":"BTCUSDT","bid1Price":"80268.50","bid1Size":"2.272",'
    b'"markPrice":"80269.43","indexPrice":"80305.30","lastPrice":"80268.60","fundingRate":"0.0001","nextFundingTime":"1793836800000",'
    b'"openInterest":"67664.089","openInterestValue":"5431357855.50","fundingIntervalHour":"8","fundingCap":"0.00333"},'
    b'"cs":838875713737,"ts":1793819857620}'
    b'{"topic":"publicTrade.BTCUSDT","type":"snapshot","ts":1793819950486,"data":[{"T":1793819950486,"s":"BTCUSDT","S":"Buy",'
    b'"v":"0.001","p":"80284.90","L":"ZeroMinusTick","i":"2dc77837-380c-51fb-8be6-f679d17ffca8","BT":false,"RPI":false,"seq":838876282470}]}'
    b'{"success":true,"ret_msg":"pong","op":"ping"}{"success":true,"ret_msg":"subscribe","op":"subscribe"}'
    b'{"event":"connected"}{"event":"disconnected"}{"endpoint":"/v5/market/orderbook","http_status":200,"params":{"category":"linear"}'
)


def _compress(data: bytes) -> bytes:
    co = zlib.compressobj(6, zdict=_ZDICT)
    return co.compress(data) + co.flush()


def _decompress(blob: bytes) -> bytes:
    do = zlib.decompressobj(zdict=_ZDICT)
    return do.decompress(blob) + do.flush()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS log (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    wall_ms INTEGER NOT NULL,
    mono_s REAL NOT NULL,
    category TEXT NOT NULL,
    conn_id TEXT NOT NULL,
    topic TEXT NOT NULL,
    payload BLOB NOT NULL,
    chain BLOB NOT NULL
);
CREATE TRIGGER IF NOT EXISTS log_no_update BEFORE UPDATE ON log
BEGIN SELECT RAISE(ABORT, 'market data log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS log_no_delete BEFORE DELETE ON log
BEGIN SELECT RAISE(ABORT, 'market data log is append-only'); END;
"""


@dataclass(frozen=True)
class Record:
    id: int
    kind: str
    wall_ms: int
    mono_s: float
    category: str
    conn_id: str
    topic: str
    payload: str
    sha256: str = ""
    chain: str = ""


class StoreError(RuntimeError):
    pass


def _utc_day(wall_ms: int) -> str:
    return datetime.fromtimestamp(wall_ms / 1000.0, tz=timezone.utc).strftime("%Y%m%d")


def _chain(prev: str, kind: str, wall_ms: int, mono_s: float, category: str, conn_id: str,
           topic: str, sha: str) -> str:
    """Hex chain value (the log stores it as 32 bytes)."""
    h = hashlib.sha256()
    h.update("|".join((prev, kind, str(wall_ms), repr(float(mono_s)), category, conn_id, topic,
                       sha)).encode("utf-8"))
    return h.hexdigest()


def _code_root() -> str:
    """The tree this code lives in: the nearest ancestor holding a `.git`, else the
    code directory itself (the production image has no `.git`; its code directory
    is /app, which also holds the trading state)."""
    here = os.path.realpath(os.path.dirname(os.path.abspath(__file__)))
    d = here
    while True:
        if os.path.exists(os.path.join(d, ".git")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return here
        d = parent


def _forbidden(path: str) -> Optional[str]:
    """Why a store may not live at `path`, or None. Symlinks are resolved first,
    so a link into the source tree is judged by where it lands."""
    p = os.path.realpath(path)
    if os.path.basename(p) == "trading_state.db":
        return "the trading runtime database"
    root = _code_root()
    if p == root or p.startswith(root + os.sep):
        return f"the source tree ({root})"
    return None


class MarketStore:
    """Open (creating) a store directory and append/iterate its records."""

    def __init__(self, directory: str, *, commit_every: int = 200,
                 commit_interval_s: float = 1.0, environment: Optional[str] = None,
                 min_free_bytes: int = 0) -> None:
        bad = _forbidden(directory)
        if bad:
            raise StoreError(f"refusing market-data store at {directory!r}: it is inside {bad}")
        os.makedirs(directory, exist_ok=True)
        self.dir = os.path.abspath(directory)
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._day: Optional[str] = None
        self._pending = 0
        self._last_commit = time.monotonic()
        #: wall-clock ms of the last SUCCESSFUL commit of a non-empty batch (None until one has happened): the recorder's "last durable write" for health reporting. Never set by a failed commit.
        self.last_commit_wall_ms: Optional[int] = None
        self.commit_every = commit_every
        self.commit_interval_s = commit_interval_s
        self._lock_fd: Optional[int] = None
        self._latest_day: str = ""
        #: Free space on the volume below which the store stops writing (0 = no
        #: reserve). The feed shares its volume with the trading ledger on a
        #: one-volume deployment; a feed that fills the disk would take the ledger's
        #: writes down with it. Refusing HERE is the defined, early failure: the
        #: engine records a store fault, entries stop, held positions are unaffected.
        self.min_free_bytes = max(0, int(min_free_bytes))
        self._free_checked_at = float("-inf")
        self._free_ok_cached = True
        #: "mainnet" | "testnet" | None (a read-only handle that does not care). A
        #: store holds ONE environment: mainnet and testnet prices must never meet.
        self.environment = self._check_environment(environment)
        self._writable = environment is not None
        if environment is not None:
            self._take_writer_lock()           # exactly one writer per directory
        self._next_id, self._prev_chain = self._scan_tail()
        self._committed_next, self._committed_chain = self._next_id, self._prev_chain       # the last state that is DURABLE: a failed commit rolls back to it
        self.emergency_prunes = 0
        self.emergency_prunes_denied = 0
        self.last_rollback_lost = 0                              # read-and-reset by the engine: how many rows a failed commit dropped
        self._last_emergency: Optional[float] = None
        #: time.monotonic() when retention first saw the volume below the write reserve, None while
        #: it is not. A stepped wall clock cannot move it: it is how long the stall has REALLY lasted.
        self._stalled_since: Optional[float] = None
        segs = self.segments()
        if segs:
            self._latest_day = segs[-1][3:11]

    # -- segments ---------------------------------------------------------------------

    def segments(self) -> List[str]:
        return sorted(f for f in os.listdir(self.dir) if f.startswith("md-") and f.endswith(".db"))

    def segment_bytes(self) -> Dict[str, int]:
        """On-disk bytes per UTC day (``YYYYMMDD``): the database plus its WAL and SHM files, from ``os.stat`` only (cheap; never opens a segment). A file that vanishes between the listing and the stat is skipped."""
        out: Dict[str, int] = {}
        try:
            names = os.listdir(self.dir)
        except OSError:
            return out
        for f in names:
            if not f.startswith("md-"):
                continue
            day = f[3:11]
            if len(day) != 8 or not day.isdigit() or f[11:] not in (".db", ".db-wal", ".db-shm"):
                continue
            try:
                out[day] = out.get(day, 0) + os.stat(os.path.join(self.dir, f)).st_size
            except OSError:
                continue
        return dict(sorted(out.items()))

    def _segment_path(self, day: str) -> str:
        return os.path.join(self.dir, f"md-{day}.db")

    def _initialised(self, name: str) -> bool:
        """False for a segment the recorder died while CREATING (the file exists, its schema does not: a 0-byte file, or a database without its ``meta`` / ``log`` tables). Such a file holds no
        record by construction, and the writer completes it idempotently (``CREATE TABLE IF NOT EXISTS``) when it next opens that day. Anything else is True, INCLUDING a file that is not a database
        at all or cannot be opened: corruption stays an error for the caller to raise, it is never mistaken for an unfinished creation."""
        path = os.path.join(self.dir, name)
        try:
            if os.path.getsize(path) == 0:
                return False
        except OSError:
            return True
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            finally:
                conn.close()
        except sqlite3.DatabaseError:
            return True
        return {"meta", "log"} <= tables

    def _abandoned(self, name: str) -> bool:
        """True for an uninitialised segment that held NO record: an unfinished CREATION (the recorder died while making the day it was about to write). That is certain for the NEWEST segment. An
        uninitialised segment with a successor is either that creation followed by later days after a restart (the file simply stayed behind) or a closed day whose file lost its content; the hash chain
        tells them apart, because it is global: if a segment had held records, the next segment's ``chain_start`` would be the chain AFTER them. When the chain continues across the file (the nearest
        earlier record's chain is exactly the nearest later segment's ``chain_start``) nothing was ever in it and it is skipped like the newest one; when it does not, its records are LOST and every
        reader must say so loudly instead of counting around the hole (an independent review zeroed a middle segment and ``count()`` quietly returned 15 records instead of 20, and a second one showed
        that treating EVERY middle file as damage bricked the recorder's own ``refresh_count`` after a crash during creation followed by a day without data).

        THE LIMIT, stated: an unfinished creation and a file that LOST its content (a power cut that left a zero-length file the day it was being written) look identical from here, and once the
        writer has continued from the last readable record the chain is re-linked past it, so even the chain cannot tell them apart afterwards. The newest file and a trailing run are skipped on
        trust; a file between records is skipped only when the chain continues across it. Every skipped file is still NAMED by ``verify()`` (never silently counted around), and if it held
        records they are gone and their ids will be reused."""
        segs = self.segments()
        if not segs or name not in segs or self._initialised(name):
            return False
        return segs[-1] == name or self._in_trailing_dead_run(name, segs) or self._chain_continues_across(name, segs)

    def _in_trailing_dead_run(self, name: str, segs: List[str]) -> bool:
        """True when EVERY segment from ``name`` to the newest is uninitialised: a creation that kept failing across several midnights (a full disk, for one). Nothing after them can prove the chain
        continues, and nothing can prove a loss either, so they are treated like the single newest one (the independent re-review's ``[records, dead, dead]`` made ``MarketStore(dir)`` itself raise and the
        recorder unable to start). ``verify()`` still names every one of them."""
        return all(not self._initialised(n) for n in segs[segs.index(name):])

    def _chain_continues_across(self, name: str, segs: List[str]) -> bool:
        i = segs.index(name)
        before: Optional[str] = None
        for n in reversed(segs[:i]):                              # the nearest earlier segment that actually holds a record
            if self._initialised(n):
                last = self._segment_meta(n)[1]
                if last:
                    before = last
                    break
        if before is None:
            pruned = self._pruned_manifest()                      # nothing earlier survives: the chain it must continue is the pruned tail, or genesis
            before = pruned[-1]["last_chain"] if pruned else GENESIS
        for n in segs[i + 1:]:
            if self._initialised(n):
                return self._segment_meta(n)[0] == before
        return False

    def _scan_tail(self) -> Tuple[int, str]:
        segs = self.segments()
        for name in reversed(segs):
            if self._abandoned(name):
                continue                                          # an abandoned creation (the newest segment, no log yet) holds no record: read the tail from the last segment that has a log
            conn = sqlite3.connect(os.path.join(self.dir, name))
            try:
                row = conn.execute("SELECT id, chain FROM log ORDER BY id DESC LIMIT 1").fetchone()
            except sqlite3.DatabaseError as exc:
                raise StoreError(f"segment {name} unreadable: {exc}") from exc
            finally:
                conn.close()
            if row:
                return int(row[0]) + 1, bytes(row[1]).hex()
        return 1, GENESIS

    def _take_writer_lock(self) -> None:
        fd = os.open(os.path.join(self.dir, "LOCK"), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise StoreError(f"{self.dir} already has a writer: {exc}") from exc
        self._lock_fd = fd

    def _env_file(self) -> str:
        return os.path.join(self.dir, "ENVIRONMENT")

    def _check_environment(self, wanted: Optional[str]) -> Optional[str]:
        """A store directory belongs to ONE environment from the moment it is
        created (before any record exists): mainnet and testnet prices must never
        share a log, a replay or a verification."""
        if wanted is not None and wanted not in ("mainnet", "testnet"):
            raise StoreError(f"environment must be mainnet or testnet, got {wanted!r}")
        held = None
        try:
            with open(self._env_file()) as fh:
                held = fh.read().strip() or None
        except FileNotFoundError:
            pass
        for name in self.segments():
            if not self._initialised(name):
                continue                                          # no meta yet: it belongs to no environment until the writer completes it (under THIS store's)
            conn = sqlite3.connect(f"file:{os.path.join(self.dir, name)}?mode=ro", uri=True)
            try:
                row = conn.execute("SELECT value FROM meta WHERE key='environment'").fetchone()
            finally:
                conn.close()
            env = row[0] if row else None
            if env is not None and held is not None and env != held:
                raise StoreError(f"segment {name} holds {env} data but this store is {held}")
            held = held or env
        if wanted is not None and held is not None and held != wanted:
            raise StoreError(f"this store holds {held} data; refusing to write {wanted} into it")
        if wanted is not None and held is None:
            with open(self._env_file(), "w") as fh:
                fh.write(wanted + "\n")
        return wanted if wanted is not None else held

    def _open(self, day: str) -> sqlite3.Connection:
        if self._conn is not None and self._day == day:
            return self._conn
        self._commit_locked()
        if self._conn is not None:
            self._conn.close()
        conn = sqlite3.connect(self._segment_path(day), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SCHEMA)
        conn.execute("INSERT OR IGNORE INTO meta VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
        conn.execute("INSERT OR IGNORE INTO meta VALUES ('day', ?)", (day,))
        conn.execute("INSERT OR IGNORE INTO meta VALUES ('dict_version', ?)", (str(DICT_VERSION),))
        if self.environment:
            conn.execute("INSERT OR IGNORE INTO meta VALUES ('environment', ?)", (self.environment,))
        # the chain value this segment continues from, so a segment can be
        # verified (and its predecessors pruned) without the earlier files
        conn.execute("INSERT OR IGNORE INTO meta VALUES ('chain_start', ?)", (self._prev_chain,))
        conn.commit()
        self._conn, self._day = conn, day
        return conn

    # -- writing ----------------------------------------------------------------------------

    def append(self, kind: str, *, wall_ms: int, mono_s: float, category: str = "",
               conn_id: str = "", topic: str = "", payload: str) -> Record:
        if not self._writable:
            raise StoreError("this store was opened read-only (no environment given): it cannot append")
        if kind not in KINDS:
            raise StoreError(f"unknown record kind {kind!r}")
        if not isinstance(payload, str):
            raise StoreError("payload must be the exact text received")
        data = payload.encode("utf-8")
        sha = hashlib.sha256(data).hexdigest()
        self._require_free_space()
        with self._lock:
            # Never back to an earlier segment: a wall clock stepped backwards (or a
            # thread racing across midnight) must not interleave two files.
            self._latest_day = max(self._latest_day, _utc_day(wall_ms))
            conn = self._open(self._latest_day)
            rid = self._next_id
            chain = _chain(self._prev_chain, kind, wall_ms, mono_s, category, conn_id, topic, sha)
            conn.execute(
                "INSERT INTO log (id, kind, wall_ms, mono_s, category, conn_id, topic, payload, "
                "chain) VALUES (?,?,?,?,?,?,?,?,?)",
                (rid, kind, int(wall_ms), float(mono_s), category, conn_id, topic,
                 _compress(data), bytes.fromhex(chain)))
            self._next_id, self._prev_chain = rid + 1, chain
            self._pending += 1
            if (self._pending >= self.commit_every
                    or time.monotonic() - self._last_commit >= self.commit_interval_s):
                self._commit_locked()
            return Record(rid, kind, int(wall_ms), float(mono_s), category, conn_id, topic,
                          payload, sha, chain)

    def free_bytes(self) -> int:
        st = os.statvfs(self.dir)
        return int(st.f_bavail) * int(st.f_frsize)

    def _require_free_space(self) -> None:
        """Raise StoreError when the volume is below the reserve. Looked at most once a
        second (a statvfs per frame would cost more than the frame); a failed look is
        itself a refusal, never 'assume there is room'."""
        if not self.min_free_bytes:
            return
        now = time.monotonic()
        if now - self._free_checked_at >= 1.0:
            self._free_checked_at = now
            try:
                self._free_ok_cached = self.free_bytes() >= self.min_free_bytes
            except OSError:
                self._free_ok_cached = False
        if not self._free_ok_cached:
            raise StoreError(f"free space on the market-data volume is below the "
                             f"{self.min_free_bytes}-byte reserve; not writing")

    def low_space(self) -> bool:
        """True when the volume is below the reserve (a failed look counts as low: never assume there is room)."""
        if not self.min_free_bytes:
            return False
        try:
            return self.free_bytes() < self.min_free_bytes
        except OSError:
            return True

    def emergency_prune(self, may_prune: Optional[Callable[[str], bool]] = None, *, min_interval_s: float = 60.0) -> List[str]:
        """Free space when the reserve is breached: remove the OLDEST closed segment (never the newest, never the one being written, only when at least two closed segments exist), with the
        usual ``pruned.jsonl`` checkpoint so the retained log still verifies. DEFAULT-DENY: without ``may_prune`` (a predicate that proves THIS segment has a verified off-volume copy) nothing
        is deleted and the refusal is counted in ``emergency_prunes_denied`` — a deleted unbacked day is unrecoverable evidence, and a stalled recorder is the lesser loss. At most one
        segment per ``min_interval_s``. Never invents or rewrites data."""
        self.flush()
        closed = [n for n in self.segments()[:-1] if n[3:11] != self._day]
        if len(closed) < 2:
            return []
        now = time.monotonic()
        if self._last_emergency is not None and now - self._last_emergency < min_interval_s:
            return []
        victim = closed[0]
        try:
            ok = bool(may_prune(victim)) if may_prune is not None else False
        except Exception:  # noqa: BLE001 - a predicate that cannot answer is a no
            ok = False
        if not ok:
            self.emergency_prunes_denied += 1
            return []
        oldest_day = datetime.strptime(victim[3:11], "%Y%m%d").replace(tzinfo=timezone.utc)
        removed = self.prune_segments_before((oldest_day + timedelta(days=1)).strftime("%Y%m%d"))
        if removed:
            self.emergency_prunes += len(removed)
            self._last_emergency = now
            self._free_checked_at = 0.0                          # look at the space again on the very next write
        return removed

    def apply_retention(self, now_ms: int, retention_days: int) -> List[str]:
        """Prune whole closed segments older than `retention_days` UTC days (0 = keep
        everything). The chain checkpoint is written by `prune_segments_before`, so
        the retained log still verifies and replays from it."""
        if retention_days <= 0:
            return []
        cutoff = _utc_day(int(now_ms) - int(retention_days) * 86_400_000)
        # A wall clock that jumps forward must not delete everything: the cutoff is also
        # bounded by the newest segment already on disk (the log's own idea of "now").
        # One exception, bounded to ONE day: while the volume is below the write reserve the log
        # cannot open the next day's segment, so its own idea of "now" stops advancing and the
        # newest-segment bound would keep every retained day forever (a stalled recorder could not
        # recover by itself: the day that is now past retention is never dropped, so space is never
        # freed, so writes never resume). A stalled store may therefore follow the wall clock by one
        # day beyond its newest segment: at most ONE extra segment (the one expiring at the next day
        # boundary) is dropped, even for a clock stepped years ahead; never a segment inside the
        # retention window measured from the wall clock, and only while stalled. A failed free-space
        # look is NOT "stalled" here (unlike the writer, which treats it as low): an error must never
        # enable a deletion.
        segs = [n for n in self.segments() if self._initialised(n)]       # an abandoned creation (possibly dated by a wrong clock) must not move the clock-jump bound
        if segs:
            newest_ms = int(datetime.strptime(segs[-1][3:11], "%Y%m%d").replace(tzinfo=timezone.utc).timestamp()) * 1000
            stalled = False
            if self.min_free_bytes:
                try:
                    stalled = self.free_bytes() < self.min_free_bytes
                except OSError:
                    stalled = False
            # The allowance is ONE day while the stall is new, plus one more for every full day the
            # stall has LASTED on the monotonic clock. It used to be one day for good, so a store whose
            # retained days could not be freed by ONE dropped day (two segments, retention 2) never
            # recovered however many days went by: each day past retention stayed, so space was never
            # freed, so writes never resumed. A wall clock stepped ahead cannot grow it (the stall's
            # age is monotonic), the cutoff is still never inside the retention window measured from the
            # wall clock, and the newest segment is never removed.
            slack_days = 0
            if stalled:
                now_mono = time.monotonic()
                if self._stalled_since is None:
                    self._stalled_since = now_mono
                slack_days = 1 + max(0, int((now_mono - self._stalled_since) // 86_400.0))
            else:
                self._stalled_since = None
            slack_ms = slack_days * 86_400_000
            cutoff = min(cutoff, _utc_day(newest_ms + slack_ms - int(retention_days) * 86_400_000))
        return self.prune_segments_before(cutoff)

    def _commit_locked(self) -> None:
        """Commit the open batch. A FAILED commit (a full volume, an I/O error) rolls the transaction back and restores the id and chain to the last DURABLE state, so a later successful
        commit can never persist rows the engine already reported as unlogged, and ids stay contiguous. The batch is lost (the engine's ``store_gap`` marker invalidates the books)."""
        if self._conn is not None and self._pending:
            try:
                self._conn.commit()
            except Exception:
                try:
                    self._conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
                self.last_rollback_lost = max(self.last_rollback_lost, self._pending)     # rows (incl. the one being appended) that were NEVER durable
                self._next_id, self._prev_chain, self._pending = self._committed_next, self._committed_chain, 0
                self._last_commit = time.monotonic()
                raise
        had_batch = self._pending > 0 and self._conn is not None
        self._committed_next, self._committed_chain = self._next_id, self._prev_chain
        self._pending = 0
        self._last_commit = time.monotonic()
        if had_batch:
            self.last_commit_wall_ms = int(time.time() * 1000)

    def flush(self) -> None:
        with self._lock:
            self._commit_locked()

    def close(self) -> None:
        with self._lock:
            self._commit_locked()
            if self._conn is not None:
                self._conn.close()
                self._conn = None
            if self._lock_fd is not None:
                try:
                    os.close(self._lock_fd)         # releases the flock
                finally:
                    self._lock_fd = None

    # -- reading --------------------------------------------------------------------------------

    def iter_records(self, *, after_id: int = 0, kinds: Optional[Tuple[str, ...]] = None
                     ) -> Iterator[Record]:
        """Records in id order across every segment (read-only connections)."""
        self.flush()
        for name in self.segments():
            if self._abandoned(name):
                continue                                          # an abandoned creation (the newest segment) holds no record; an emptied OLDER segment raises below
            conn = sqlite3.connect(f"file:{os.path.join(self.dir, name)}?mode=ro", uri=True)
            try:
                cur = conn.execute(
                    "SELECT id, kind, wall_ms, mono_s, category, conn_id, topic, payload, "
                    "chain FROM log WHERE id > ? ORDER BY id", (after_id,))
                for row in cur:
                    if kinds and row[1] not in kinds:
                        continue
                    text = _decompress(row[7]).decode("utf-8")
                    yield Record(row[0], row[1], row[2], row[3], row[4], row[5], row[6], text,
                                 hashlib.sha256(text.encode("utf-8")).hexdigest(),
                                 bytes(row[8]).hex())
            finally:
                conn.close()

    def newest_id(self) -> int:
        """The id of the newest record that is DURABLE (committed; 0 = none), read WITHOUT the writer lock and without touching the disk: a liveness/progress counter for a heartbeat that must
        never wait on the writer or scan the log (``count()`` flushes and scans every segment)."""
        return int(self._committed_next) - 1

    def count(self) -> int:
        self.flush()
        total = 0
        for name in self.segments():
            if self._abandoned(name):
                continue                                          # an abandoned creation (the newest segment) holds no record; an emptied OLDER segment raises below
            conn = sqlite3.connect(f"file:{os.path.join(self.dir, name)}?mode=ro", uri=True)
            try:
                total += conn.execute("SELECT COUNT(*) FROM log").fetchone()[0]
            finally:
                conn.close()
        return total

    def _segment_meta(self, name: str) -> Tuple[str, str]:
        conn = sqlite3.connect(f"file:{os.path.join(self.dir, name)}?mode=ro", uri=True)
        try:
            start = conn.execute("SELECT value FROM meta WHERE key='chain_start'").fetchone()
            last = conn.execute("SELECT chain FROM log ORDER BY id DESC LIMIT 1").fetchone()
        finally:
            conn.close()
        return (start[0] if start else GENESIS), (bytes(last[0]).hex() if last else "")

    def verify(self) -> List[str]:
        """Recompute the chain over every retained segment. Each segment's
        ``chain_start`` must equal the previous retained segment's last chain, or
        (for the oldest retained one) the last chain recorded when its
        predecessors were pruned. Returns problems (empty = consistent)."""
        problems: List[str] = []
        self.flush()
        expect_id = None
        prev_last: Optional[str] = None
        pruned = self._pruned_manifest()
        for idx, name in enumerate(self.segments()):
            if not self._initialised(name):
                if self._abandoned(name):
                    segs_now = self.segments()
                    if name == segs_now[-1]:
                        why = ""
                    elif self._in_trailing_dead_run(name, segs_now):
                        why = ("; it is part of a trailing run of unfinished creations: nothing after it can prove whether it ever held a record, so it is treated like the newest one "
                               "(check the recorder's counters, move it aside when convenient, RUNBOOKS section 14)")
                    else:
                        why = ("; the hash chain continues across it: if the recorder died while creating it, it held no record; if the file lost its content the writer has since re-linked the chain "
                               "past it and the store cannot tell the two apart (check the monitor's id counter; move it aside when convenient, RUNBOOKS section 14)")
                    problems.append(f"{name}: uninitialised segment (the recorder died while creating it: no log table); it holds no record" + why)
                else:
                    problems.append(f"{name}: EMPTY segment that is not the newest and the hash chain does NOT continue across it: its records are LOST (damage); readers refuse it. "
                                    f"Check, then move it aside deliberately (RUNBOOKS section 14)")
                continue
            start, _ = self._segment_meta(name)
            if idx == 0:
                if pruned:
                    if start != pruned[-1]["last_chain"]:
                        problems.append(f"{name}: starts from {start[:12]}, not the pruned tail "
                                        f"{pruned[-1]['last_chain'][:12]}")
                elif start != GENESIS:
                    problems.append(f"{name}: history before it is missing and no prune is recorded")
            elif prev_last is not None and start != prev_last:
                problems.append(f"{name}: chain_start does not continue the previous segment")
            prev = start
            conn = sqlite3.connect(f"file:{os.path.join(self.dir, name)}?mode=ro", uri=True)
            try:
                for row in conn.execute("SELECT id, kind, wall_ms, mono_s, category, conn_id, topic, "
                                        "payload, chain FROM log ORDER BY id"):
                    rid = row[0]
                    if expect_id is not None and rid != expect_id:
                        problems.append(f"id gap: expected {expect_id}, found {rid}")
                    expect_id = rid + 1
                    try:
                        text = _decompress(row[7]).decode("utf-8")
                    except (zlib.error, UnicodeDecodeError):
                        problems.append(f"record {rid}: payload unreadable")
                        text = ""
                    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
                    want = _chain(prev, row[1], row[2], row[3], row[4], row[5], row[6], sha)
                    got = bytes(row[8]).hex()
                    if want != got:
                        problems.append(f"record {rid}: chain mismatch (payload or fields altered)")
                    prev = got
            finally:
                conn.close()
            prev_last = prev
        return problems

    # -- retention ---------------------------------------------------------------------------

    def _manifest_path(self) -> str:
        return os.path.join(self.dir, "pruned.jsonl")

    def _pruned_manifest(self) -> List[dict]:
        try:
            with open(self._manifest_path()) as fh:
                return [json.loads(line) for line in fh if line.strip()]
        except FileNotFoundError:
            return []

    def prune_segments_before(self, day: str) -> List[str]:
        """Delete whole CLOSED segments dated before `day` (YYYYMMDD). The newest
        segment and the one being written are never removed. Each removal is
        recorded in ``pruned.jsonl`` with the last chain value, so the retained
        log still verifies from that checkpoint. Retention is an operator
        decision; nothing here calls this automatically."""
        self.flush()
        segs = self.segments()
        removable = [n for n in segs[:-1] if n[3:11] < day and n[3:11] != self._day]
        removed: List[str] = []
        for name in removable:
            if removed == [] and name != segs[0]:
                break                       # only ever prune from the oldest end
            if not self._initialised(name):                    # an emptied segment holds no record: nothing to checkpoint. It is removed WITH an audit row, never silently
                before = self._pruned_manifest()
                with open(self._manifest_path(), "a") as fh:
                    fh.write(json.dumps({"segment": name, "records": 0, "uninitialised": True, "last_chain": before[-1]["last_chain"] if before else GENESIS,
                                         "pruned_at_ms": int(time.time() * 1000)}, sort_keys=True) + "\n")
                for suffix in ("", "-wal", "-shm"):
                    try:
                        os.remove(os.path.join(self.dir, name + suffix))
                    except FileNotFoundError:
                        pass
                removed.append(name)
                segs = self.segments()
                continue
            _, last = self._segment_meta(name)
            conn = sqlite3.connect(f"file:{os.path.join(self.dir, name)}?mode=ro", uri=True)
            try:
                # ids are one contiguous sequence in an append-only log, so the count is
                # max-min+1, read from the primary-key index. COUNT(*) would scan a
                # segment of ~1.5 GB while the caller may hold the engine lock.
                lo, hi = conn.execute("SELECT MIN(id), MAX(id) FROM log").fetchone()
                count = 0 if lo is None else int(hi) - int(lo) + 1
            finally:
                conn.close()
            with open(self._manifest_path(), "a") as fh:
                fh.write(json.dumps({"segment": name, "records": count, "last_chain": last,
                                     "pruned_at_ms": int(time.time() * 1000)}, sort_keys=True) + "\n")
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(os.path.join(self.dir, name + suffix))
                except FileNotFoundError:
                    pass
            removed.append(name)
            segs = self.segments()
        return removed


def view_payload(view_dict: dict, watermark_id: int) -> str:
    """The canonical text of a logged view (sorted keys, so it is comparable)."""
    return json.dumps({"watermark": watermark_id, "view": view_dict}, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)
