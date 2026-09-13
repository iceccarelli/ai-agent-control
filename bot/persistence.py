"""persistence.py — the single durable state store.

WHY THIS MODULE EXISTS
======================
The legacy codebase kept every piece of safety state in memory:

* ``peak_balance`` re-seeded from ``0.0`` on construction, so the first tick
  after a restart always computed ``current_drawdown == 0.0``.  Drawdown
  protection therefore died precisely during an ECS crash-loop, which is
  exactly when it is needed.
* Circuit-breaker state, cooldowns, daily anchors and consecutive-loss counts
  were plain instance attributes, so a restart cleared every brake.
* Position counts reset to zero, so the bot stacked new positions on top of
  ones it had forgotten.

Everything that must survive ``kill -9`` lives here, in SQLite, committed
synchronously.

DESIGN RULES
------------
1. **Fail closed.**  If the store cannot be opened or read, callers must treat
   that as "brakes engaged", never as "no limits recorded".  Every read helper
   therefore raises rather than returning a permissive default; the caller
   decides, and the risk layer's decision is always to block.
2. **UTC only.**  Day boundaries are ``YYYY-MM-DD`` in UTC.  The legacy code
   mixed ``datetime.now()`` (local) for ``get_daily_return`` with UTC for the
   breaker key, so the two reset on different days.
3. **Fractions only.**  Any ratio stored here is a fraction (0.02 == 2%).
4. **Monotonic vs wall clock.**  Deadlines that must survive a restart
   (cooldown expiry) are stored as epoch seconds, never ``perf_counter()``.
   The legacy code stored ``perf_counter()`` deadlines, which reset to ~0 on
   restart and so expired instantly.
5. **Single writer per process.**  A re-entrant lock guards the connection;
   ``check_same_thread=False`` plus the lock lets the trading loop and the
   monitor thread share one store safely.

The kill switch is deliberately one-way from the bot's side: :meth:`trip_kill_switch`
sets it, and nothing in this codebase clears it.  Clearing is
:meth:`clear_kill_switch_by_human`, which requires an explicit operator token and
is never called by automated code paths.

THE MEMORY TABLES (schema v2)
-----------------------------
``execution_quality``, ``regime_history`` and ``memory_kv`` were added so the
bot can *remember* what actually happened, not merely what it currently holds.
They follow the same rules as everything above — explicit schema, epoch
timestamps, atomic commits — and they are deliberately **additive**: no existing
table or method signature changed, because eight other modules depend on them.

They are also deliberately *not* a second source of truth.  ``regime_history``
records the regime label observed at a moment in time and nothing else; the
outcome of the trades opened in that regime is derived by joining back to
``trades`` (:meth:`trades_with_regime`).  A denormalised outcome column would be
a copy of the ledger that can silently drift from it, and a ledger with a rival
is not a ledger.  ``execution_quality`` is the one genuinely new fact: what we
*intended* versus what we *got* is nowhere else in the store, and it is the
only way to find out whether the cost model matches reality.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "StateStore",
    "PersistenceError",
    "utc_day",
    "utc_now_epoch",
    "DEFAULT_DB_PATH",
    "MEMORY_KEY_CONVENTION",
    "slippage_bps",
]

DEFAULT_DB_PATH = os.environ.get("STATE_DB_PATH", "state/trading_state.db")

#: v1 -> v2 added the memory tables (execution_quality, regime_history,
#: memory_kv).  The bump is informational: every statement in ``_DDL`` is
#: ``IF NOT EXISTS``, so opening a v1 database simply grows the new tables.
_SCHEMA_VERSION = 2

#: Namespace/key convention for :meth:`StateStore.memory_put`.
#:
#: ``namespace`` is the *module that owns the fact* — ``"engine"``,
#: ``"strategy"``, ``"risk"``, ``"ops"``, ``"exec"``.  ``key`` is a snake_case
#: noun, optionally suffixed with the symbol it is about:
#: ``"last_regime:BTCUSDT"``.  Values must be JSON-serialisable; they are stored
#: as JSON text so a reader never has to guess a type.
#:
#: Namespaces beginning with ``"_"`` are reserved for the memory layer itself.
#: Nothing that resembles a credential belongs here — see
#: ``memory.TradingMemory.remember``, which refuses such keys outright.
MEMORY_KEY_CONVENTION = "<module>/<snake_case_noun>[:<SYMBOL>] -> JSON value"


class PersistenceError(RuntimeError):
    """Raised when durable state cannot be read or written.

    Callers must treat this as a blocking condition.  A risk gate that cannot
    read its own breaker state does not know whether it is tripped, and the only
    safe interpretation of "I don't know" is "no".
    """


def utc_now_epoch() -> float:
    """Wall-clock epoch seconds (UTC).  Survives restarts, unlike perf_counter."""
    return time.time()


def utc_day(ts: Optional[float] = None) -> str:
    """UTC day key, ``YYYY-MM-DD``.  The one day-boundary definition."""
    dt = datetime.fromtimestamp(utc_now_epoch() if ts is None else ts, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BreakerState:
    """Circuit-breaker state as persisted.

    ``cooldown_until_epoch`` is wall-clock epoch seconds so that a cooldown set
    before a restart is still in force after it.
    """

    active: bool = False
    reason: str = ""
    tripped_at_epoch: float = 0.0
    cooldown_until_epoch: float = 0.0
    consecutive_losses: int = 0

    def is_cooling_down(self, now: Optional[float] = None) -> bool:
        return (now if now is not None else utc_now_epoch()) < self.cooldown_until_epoch


@dataclass(frozen=True)
class DailyAnchor:
    """The equity the day started at, keyed by UTC day."""

    day: str
    start_equity: float
    realised_pnl: float = 0.0
    trades: int = 0


@dataclass(frozen=True)
class EquityState:
    """Peak equity for drawdown, and the most recent observation.

    Peak is taken over EQUITY (cash + position value), never available balance:
    opening a position mechanically reduces available balance, so a peak taken
    from it makes drawdown rise the instant you enter a trade.
    """

    peak_equity: float = 0.0
    last_equity: float = 0.0
    updated_epoch: float = 0.0

    def drawdown_fraction(self, equity: Optional[float] = None) -> float:
        """Current drawdown as a FRACTION of peak (0.05 == 5% below peak)."""
        eq = self.last_equity if equity is None else equity
        if self.peak_equity <= 0.0:
            return 0.0
        return max(0.0, (self.peak_equity - eq) / self.peak_equity)


@dataclass
class TradeRecord:
    """One closed trade, fee-inclusive.

    ``net_pnl`` is AFTER both entry and exit fees.  The legacy analytics
    computed win rate on gross PnL, which inflated the Kelly input.
    """

    symbol: str
    side: str
    qty: float
    entry_price: float
    exit_price: float
    gross_pnl: float
    entry_fee: float
    exit_fee: float
    opened_epoch: float
    closed_epoch: float
    order_link_id: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def total_fees(self) -> float:
        return float(self.entry_fee) + float(self.exit_fee)

    @property
    def net_pnl(self) -> float:
        return float(self.gross_pnl) - self.total_fees

    @property
    def is_win(self) -> bool:
        """Fee-aware: a trade that made money gross but lost after fees is a LOSS."""
        return self.net_pnl > 0.0


# ---------------------------------------------------------------------------
# the store
# ---------------------------------------------------------------------------


_DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS equity_state (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    peak_equity   REAL NOT NULL DEFAULT 0.0,
    last_equity   REAL NOT NULL DEFAULT 0.0,
    updated_epoch REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS daily_anchor (
    day          TEXT PRIMARY KEY,
    start_equity REAL NOT NULL,
    realised_pnl REAL NOT NULL DEFAULT 0.0,
    trades       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS breaker (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),
    active              INTEGER NOT NULL DEFAULT 0,
    reason              TEXT NOT NULL DEFAULT '',
    tripped_at_epoch    REAL NOT NULL DEFAULT 0.0,
    cooldown_until_epoch REAL NOT NULL DEFAULT 0.0,
    consecutive_losses  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS kill_switch (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    engaged       INTEGER NOT NULL DEFAULT 0,
    reason        TEXT NOT NULL DEFAULT '',
    engaged_epoch REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS symbol_cooldown (
    symbol            TEXT PRIMARY KEY,
    until_epoch       REAL NOT NULL,
    reason            TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS trades (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol         TEXT NOT NULL,
    side           TEXT NOT NULL,
    qty            REAL NOT NULL,
    entry_price    REAL NOT NULL,
    exit_price     REAL NOT NULL,
    gross_pnl      REAL NOT NULL,
    entry_fee      REAL NOT NULL,
    exit_fee       REAL NOT NULL,
    net_pnl        REAL NOT NULL,
    opened_epoch   REAL NOT NULL,
    closed_epoch   REAL NOT NULL,
    day            TEXT NOT NULL,
    order_link_id  TEXT NOT NULL DEFAULT '',
    meta           TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_trades_day ON trades(day);
CREATE INDEX IF NOT EXISTS idx_trades_closed ON trades(closed_epoch);

CREATE TABLE IF NOT EXISTS positions (
    symbol        TEXT PRIMARY KEY,
    side          TEXT NOT NULL,
    qty           REAL NOT NULL,
    entry_price   REAL NOT NULL,
    stop_price    REAL NOT NULL DEFAULT 0.0,
    opened_epoch  REAL NOT NULL,
    order_link_id TEXT NOT NULL DEFAULT '',
    meta          TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS orders (
    order_link_id TEXT PRIMARY KEY,
    symbol        TEXT NOT NULL,
    side          TEXT NOT NULL,
    order_type    TEXT NOT NULL,
    qty           REAL NOT NULL,
    price         REAL NOT NULL DEFAULT 0.0,
    purpose       TEXT NOT NULL DEFAULT 'entry',
    status        TEXT NOT NULL DEFAULT 'pending',
    exchange_id   TEXT NOT NULL DEFAULT '',
    created_epoch REAL NOT NULL,
    updated_epoch REAL NOT NULL,
    meta          TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_orders_symbol ON orders(symbol);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);

CREATE TABLE IF NOT EXISTS order_seq (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    next_id INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS decisions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_epoch      REAL NOT NULL,
    symbol        TEXT NOT NULL,
    decision      TEXT NOT NULL,
    reason        TEXT NOT NULL,
    detail        TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(ts_epoch);

-- ------------------------------------------------------------------ memory
-- What we intended versus what we got.  Nothing else in the store records the
-- intended price, so without this table the cost model can never be checked
-- against reality: the ledger only knows the fill.
--
-- slippage_bps is NULL, not 0.0, when it cannot be computed (a rejected order
-- has no fill).  Zero is a measurement; NULL is the absence of one, and the
-- two must not be averaged together.
CREATE TABLE IF NOT EXISTS execution_quality (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_epoch       REAL NOT NULL,
    symbol         TEXT NOT NULL,
    order_link_id  TEXT NOT NULL DEFAULT '',
    side           TEXT NOT NULL,
    order_type     TEXT NOT NULL DEFAULT '',
    purpose        TEXT NOT NULL DEFAULT 'entry',
    outcome        TEXT NOT NULL DEFAULT 'filled',
    reason         TEXT NOT NULL DEFAULT '',
    intended_price REAL NOT NULL DEFAULT 0.0,
    fill_price     REAL NOT NULL DEFAULT 0.0,
    qty            REAL NOT NULL DEFAULT 0.0,
    slippage_bps   REAL,
    is_maker       INTEGER NOT NULL DEFAULT 0,
    time_to_fill_s REAL,
    meta           TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_execq_symbol ON execution_quality(symbol);
CREATE INDEX IF NOT EXISTS idx_execq_ts ON execution_quality(ts_epoch);
CREATE INDEX IF NOT EXISTS idx_execq_outcome ON execution_quality(outcome);

-- The regime observed at a moment in time, per symbol.  Observations only:
-- the outcome of trades opened under a regime is derived by joining to
-- `trades` (see trades_with_regime), never copied here.
CREATE TABLE IF NOT EXISTS regime_history (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_epoch  REAL NOT NULL,
    symbol    TEXT NOT NULL,
    regime    TEXT NOT NULL,
    detail    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_regime_symbol_ts ON regime_history(symbol, ts_epoch);

-- Namespaced key/value for facts that do not deserve their own table.
-- See MEMORY_KEY_CONVENTION.
CREATE TABLE IF NOT EXISTS memory_kv (
    namespace     TEXT NOT NULL,
    key           TEXT NOT NULL,
    value         TEXT NOT NULL,
    updated_epoch REAL NOT NULL,
    PRIMARY KEY (namespace, key)
);

-- THE CARRY POSITION (0037). One row, overwritten: what THIS process
-- believed it was holding, written the moment it changed. It is a LEDGER, not
-- a cache of the venue: cold start compares the two and refuses to trade when
-- they disagree, and never edits this row to make them agree.
CREATE TABLE IF NOT EXISTS carry_position (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    book_state    TEXT NOT NULL DEFAULT 'FLAT',
    payload       TEXT NOT NULL DEFAULT '',
    updated_epoch REAL NOT NULL DEFAULT 0.0
);

-- Additive indexes for the memory reads.  Both are over existing tables and
-- change no existing behaviour; they only stop the health thread's aggregate
-- reads from turning into full scans as the ledger grows.
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol, closed_epoch);
CREATE INDEX IF NOT EXISTS idx_decisions_reason ON decisions(decision, reason);
"""

#: Outcomes ``record_execution`` accepts.  An unknown outcome is refused rather
#: than stored, because a typo silently becomes a category nobody aggregates.
EXECUTION_OUTCOMES = ("filled", "partial", "rejected", "cancelled", "unfilled")


def slippage_bps(side: str, intended_price: float, fill_price: float) -> float:
    """Realised slippage in **basis points, positive == adverse**.

    One definition, in one place, because the sign is the entire content of the
    measurement.  Buying above the intended price costs money; selling below it
    costs money.  Both are reported as a positive number so that "mean slippage"
    means "mean cost" and a favourable fill can pull the average down rather
    than being silently counted as a cost of the same magnitude.

        Buy :  (fill - intended) / intended * 10_000
        Sell:  (intended - fill) / intended * 10_000

    Raises :class:`PersistenceError` on an unknown side or a non-positive
    reference price.  Guessing the sign of a cost is worse than not recording it.
    """
    s = str(side).strip().lower()
    if s in ("buy", "long"):
        sign = 1.0
    elif s in ("sell", "short"):
        sign = -1.0
    else:
        raise PersistenceError(
            f"cannot compute slippage for side {side!r}: the sign of the cost "
            "is not derivable from an unknown side"
        )
    ref = float(intended_price)
    got = float(fill_price)
    for name, value in (("intended_price", ref), ("fill_price", got)):
        if value != value or value in (float("inf"), float("-inf")):
            raise PersistenceError(f"non-finite {name}={value!r} in slippage")
    if ref <= 0.0:
        raise PersistenceError(
            f"intended_price must be positive to express slippage in bps, got {ref!r}"
        )
    return sign * (got - ref) / ref * 10_000.0


class StateStore:
    """Durable state for the risk engine and the order lifecycle.

    Usage::

        store = StateStore("state/trading_state.db")
        store.update_equity(10_000.0)
        store.drawdown_fraction()        # 0.0
        store.update_equity(9_000.0)
        store.drawdown_fraction()        # 0.10

    Every mutating method commits before returning, so a ``kill -9`` at any
    point leaves a consistent store.
    """

    def __init__(self, path: str = DEFAULT_DB_PATH) -> None:
        self._path = path
        self._lock = threading.RLock()
        #: Thread id permitted to mutate state; None means unrestricted.
        self._writer_thread: Optional[int] = None
        try:
            if path != ":memory:":
                parent = os.path.dirname(os.path.abspath(path))
                if parent:
                    os.makedirs(parent, exist_ok=True)
            self._conn = sqlite3.connect(path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            # WAL keeps readers unblocked and survives an abrupt kill better
            # than the default rollback journal.
            if path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.executescript(_DDL)
            self._conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(_SCHEMA_VERSION),),
            )
            # Migration is forward-only and additive.  Every statement in _DDL
            # is IF NOT EXISTS, so a database written by an older build simply
            # grows the new tables when it is opened here; _migrate then adds
            # any column that a *partially* upgraded database is missing.  A
            # store that refused to open an older database would take the
            # brakes offline at exactly the moment a rollback needed them.
            self._migrate_additive()
            self._conn.execute(
                "UPDATE meta SET value = ? WHERE key = 'schema_version'",
                (str(_SCHEMA_VERSION),),
            )
            for stmt in (
                "INSERT OR IGNORE INTO equity_state(id) VALUES(1)",
                "INSERT OR IGNORE INTO breaker(id) VALUES(1)",
                "INSERT OR IGNORE INTO kill_switch(id) VALUES(1)",
                "INSERT OR IGNORE INTO order_seq(id, next_id) VALUES(1, 1)",
            ):
                self._conn.execute(stmt)
            self._conn.commit()
        except Exception as exc:  # noqa: BLE001 - re-raised as PersistenceError
            raise PersistenceError(f"cannot open state store at {path!r}: {exc}") from exc

    # -- concurrency -------------------------------------------------------

    def claim_writer(self) -> None:
        """Declare the calling thread the single writer.

        The concurrency model is **one writer, many readers**, and this makes it
        enforceable rather than merely documented. The trading loop claims the
        role at startup; any *other* thread that then attempts a mutating call
        raises :class:`PersistenceError` instead of silently interleaving.

        This is deliberately stricter than the lock alone. A re-entrant lock
        makes concurrent writes *safe* at the SQLite level while still allowing
        two threads to interleave a read-modify-write on position state — which
        is how a book and an exchange drift apart. Serialising at the SQLite
        layer does not serialise the decision that produced the write.
        """
        with self._lock:
            self._writer_thread = threading.get_ident()

    def release_writer(self) -> None:
        with self._lock:
            self._writer_thread = None

    def _assert_writer(self) -> None:
        writer = self._writer_thread
        if writer is not None and threading.get_ident() != writer:
            raise PersistenceError(
                "a non-writer thread attempted to mutate trading state. The "
                "concurrency model is single-writer: only the thread that "
                "called claim_writer() may write. Reads are unrestricted."
            )

    # -- plumbing ---------------------------------------------------------

    #: Columns each memory table must have, checked on every open.  This is the
    #: migration: a database created by a build that had the table but not a
    #: later column is repaired in place rather than rejected.
    _MEMORY_COLUMNS: Dict[str, Dict[str, str]] = {
        "execution_quality": {
            "order_link_id": "TEXT NOT NULL DEFAULT ''",
            "order_type": "TEXT NOT NULL DEFAULT ''",
            "purpose": "TEXT NOT NULL DEFAULT 'entry'",
            "outcome": "TEXT NOT NULL DEFAULT 'filled'",
            "reason": "TEXT NOT NULL DEFAULT ''",
            "intended_price": "REAL NOT NULL DEFAULT 0.0",
            "fill_price": "REAL NOT NULL DEFAULT 0.0",
            "qty": "REAL NOT NULL DEFAULT 0.0",
            "slippage_bps": "REAL",
            "is_maker": "INTEGER NOT NULL DEFAULT 0",
            "time_to_fill_s": "REAL",
            "meta": "TEXT NOT NULL DEFAULT '{}'",
        },
        "regime_history": {"detail": "TEXT NOT NULL DEFAULT '{}'"},
        "memory_kv": {"updated_epoch": "REAL NOT NULL DEFAULT 0.0"},
    }

    def _migrate_additive(self) -> None:
        """Add any missing memory column.  Never drops, never rewrites."""
        for table, columns in self._MEMORY_COLUMNS.items():
            try:
                present = {
                    str(row["name"])
                    for row in self._conn.execute(f"PRAGMA table_info({table})")
                }
            except Exception:  # noqa: BLE001 - table absent is not an error here
                continue
            if not present:
                continue
            for name, decl in columns.items():
                if name in present:
                    continue
                # SQLite cannot add a NOT NULL column without a default; every
                # declaration above therefore carries one.
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    def schema_version(self) -> int:
        """The schema version recorded in the database, not the constant."""
        rows = self._query("SELECT value FROM meta WHERE key = 'schema_version'")
        if not rows:
            raise PersistenceError("schema_version row missing")
        return int(rows[0]["value"])

    @property
    def path(self) -> str:
        return self._path

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.commit()
            finally:
                self._conn.close()

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- dust ledger -------------------------------------------------------

    def add_dust(self, symbol: str, qty: float) -> float:
        """Record base-asset dust the book can no longer carry. A **write**.

        When a partial exit leaves a remainder below the exchange minimum, the
        engine closes the position on the books: the remainder cannot be sold
        and cannot be protected by a stop, so carrying it as an open position
        would mean reporting a position the system can neither exit nor guard.

        That is the right call, and until slice 9 it also quietly broke the
        codebase's strongest invariant — *the ledger explains the entire equity
        change*. The exchange still held the dust. The book said zero. On a
        daily BTC backtest that divergence was reported 317 times, and it grew
        as the price rose, because a fixed quantity of dust crosses a fixed
        minimum NOTIONAL as the asset appreciates.

        So the dust is not forgotten, it is *booked*. Accumulated per symbol,
        persisted, and included in every reconciliation. The system still
        cannot trade it; it can now account for it, which is a different and
        achievable promise.
        """
        # _exec already asserts the writer role, takes the lock and commits.
        # Doing any of those again here would be a second, subtly different
        # transaction discipline in a file whose whole point is having one.
        self._exec(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = "
            "CAST(CAST(meta.value AS REAL) + ? AS TEXT)",
            (f"dust:{symbol}", repr(float(qty)), float(qty)),
        )
        return self.dust(symbol)

    def dust(self, symbol: str) -> float:
        """Accumulated unsellable dust for one symbol. A read; never raises."""
        rows = self._query("SELECT value FROM meta WHERE key = ?", (f"dust:{symbol}",))
        if not rows:
            return 0.0
        try:
            return float(rows[0]["value"])
        except (TypeError, ValueError):
            return 0.0

    def all_dust(self) -> Dict[str, float]:
        rows = self._query("SELECT key, value FROM meta WHERE key LIKE 'dust:%'")
        out: Dict[str, float] = {}
        for row in rows:
            try:
                out[str(row["key"])[5:]] = float(row["value"])
            except (TypeError, ValueError):
                continue
        return out

    def _exec(self, sql: str, params: Tuple[Any, ...] = ()) -> sqlite3.Cursor:
        self._assert_writer()
        with self._lock:
            try:
                cur = self._conn.execute(sql, params)
                self._conn.commit()
                return cur
            except Exception as exc:  # noqa: BLE001
                raise PersistenceError(f"state write failed: {exc}") from exc

    def _query(self, sql: str, params: Tuple[Any, ...] = ()) -> List[sqlite3.Row]:
        with self._lock:
            try:
                return list(self._conn.execute(sql, params).fetchall())
            except Exception as exc:  # noqa: BLE001
                raise PersistenceError(f"state read failed: {exc}") from exc

    # -- equity / drawdown -------------------------------------------------

    def get_equity_state(self) -> EquityState:
        rows = self._query("SELECT * FROM equity_state WHERE id = 1")
        if not rows:
            raise PersistenceError("equity_state row missing")
        r = rows[0]
        return EquityState(
            peak_equity=float(r["peak_equity"]),
            last_equity=float(r["last_equity"]),
            updated_epoch=float(r["updated_epoch"]),
        )

    def update_equity(self, equity: float) -> EquityState:
        """Record an equity observation and ratchet the peak.

        ``equity`` must be EQUITY (cash + marked position value), not available
        balance.  Non-finite or negative values are rejected rather than stored:
        a bad reading must not silently reset the peak and erase the drawdown
        brake.
        """
        eq = float(equity)
        if not (eq == eq) or eq in (float("inf"), float("-inf")):  # NaN/inf
            raise PersistenceError(f"refusing to store non-finite equity {equity!r}")
        if eq < 0.0:
            raise PersistenceError(f"refusing to store negative equity {equity!r}")
        now = utc_now_epoch()
        self._exec(
            "UPDATE equity_state SET peak_equity = MAX(peak_equity, ?), "
            "last_equity = ?, updated_epoch = ? WHERE id = 1",
            (eq, eq, now),
        )
        # Anchor the day at the FIRST equity observation of that UTC day, not at
        # the first time somebody asks for the daily loss.  If the anchor were
        # created lazily on query, a process that restarted mid-day would anchor
        # at the already-drawn-down equity and silently reset the daily-loss
        # brake — the same failure mode as the legacy in-memory state.  Because
        # this is INSERT OR IGNORE against a persisted row, a restart inside the
        # same UTC day keeps the original anchor.
        self.ensure_daily_anchor(eq)
        return self.get_equity_state()

    def drawdown_fraction(self, equity: Optional[float] = None) -> float:
        """Drawdown from persisted peak, as a FRACTION."""
        return self.get_equity_state().drawdown_fraction(equity)

    # -- daily anchors -----------------------------------------------------

    def ensure_daily_anchor(self, equity: float, day: Optional[str] = None) -> DailyAnchor:
        """Return today's anchor, creating it from ``equity`` on first call.

        Idempotent: the anchor is written once per UTC day and never moved, so
        the daily-loss brake measures from the true start of day even across
        restarts.
        """
        d = day or utc_day()
        rows = self._query("SELECT * FROM daily_anchor WHERE day = ?", (d,))
        if not rows:
            self._exec(
                "INSERT OR IGNORE INTO daily_anchor(day, start_equity) VALUES(?, ?)",
                (d, float(equity)),
            )
            rows = self._query("SELECT * FROM daily_anchor WHERE day = ?", (d,))
        r = rows[0]
        return DailyAnchor(
            day=r["day"],
            start_equity=float(r["start_equity"]),
            realised_pnl=float(r["realised_pnl"]),
            trades=int(r["trades"]),
        )

    def get_daily_anchor(self, day: Optional[str] = None) -> Optional[DailyAnchor]:
        rows = self._query("SELECT * FROM daily_anchor WHERE day = ?", (day or utc_day(),))
        if not rows:
            return None
        r = rows[0]
        return DailyAnchor(
            day=r["day"],
            start_equity=float(r["start_equity"]),
            realised_pnl=float(r["realised_pnl"]),
            trades=int(r["trades"]),
        )

    def daily_loss_fraction(self, equity: float, day: Optional[str] = None) -> float:
        """Today's loss as a POSITIVE FRACTION of the day's starting equity.

        Returns 0.0 when up on the day.  This is the calculation the legacy code
        got wrong: it summed raw currency PnL and compared it under ``abs()``
        against a percentage threshold, so a +$5.01 profit tripped an
        "EMERGENCY STOP" while a -$50k day after a restart tripped nothing.
        """
        anchor = self.ensure_daily_anchor(equity, day)
        if anchor.start_equity <= 0.0:
            raise PersistenceError(
                "daily anchor has non-positive start equity; cannot compute "
                "daily loss fraction"
            )
        change = (float(equity) - anchor.start_equity) / anchor.start_equity
        return max(0.0, -change)

    # -- circuit breaker ---------------------------------------------------

    def get_breaker(self) -> BreakerState:
        rows = self._query("SELECT * FROM breaker WHERE id = 1")
        if not rows:
            raise PersistenceError("breaker row missing")
        r = rows[0]
        return BreakerState(
            active=bool(r["active"]),
            reason=str(r["reason"]),
            tripped_at_epoch=float(r["tripped_at_epoch"]),
            cooldown_until_epoch=float(r["cooldown_until_epoch"]),
            consecutive_losses=int(r["consecutive_losses"]),
        )

    def trip_breaker(self, reason: str, cooldown_seconds: float) -> BreakerState:
        now = utc_now_epoch()
        self._exec(
            "UPDATE breaker SET active = 1, reason = ?, tripped_at_epoch = ?, "
            "cooldown_until_epoch = MAX(cooldown_until_epoch, ?) WHERE id = 1",
            (str(reason), now, now + max(0.0, float(cooldown_seconds))),
        )
        return self.get_breaker()

    def clear_breaker(self) -> BreakerState:
        """Clear the breaker.  Only legitimate once the cooldown has expired;
        the risk layer enforces that, not the store."""
        self._exec(
            "UPDATE breaker SET active = 0, reason = '', cooldown_until_epoch = 0.0 "
            "WHERE id = 1"
        )
        return self.get_breaker()

    def set_consecutive_losses(self, n: int) -> None:
        self._exec(
            "UPDATE breaker SET consecutive_losses = ? WHERE id = 1", (max(0, int(n)),)
        )

    # -- kill switch (human-cleared only) ----------------------------------

    def is_kill_switch_engaged(self) -> Tuple[bool, str]:
        rows = self._query("SELECT * FROM kill_switch WHERE id = 1")
        if not rows:
            raise PersistenceError("kill_switch row missing")
        r = rows[0]
        return bool(r["engaged"]), str(r["reason"])

    def trip_kill_switch(self, reason: str) -> None:
        """Engage the kill switch.  The bot may call this freely.

        There is deliberately no automated path that clears it — see
        :meth:`clear_kill_switch_by_human`.
        """
        self._exec(
            "UPDATE kill_switch SET engaged = 1, reason = ?, engaged_epoch = ? "
            "WHERE id = 1",
            (str(reason), utc_now_epoch()),
        )

    # -- the carry book's position ----------------------------------------

    def save_carry_position(self, state: Dict[str, Any]) -> None:
        """Record what the carry book holds. Called on every state change.

        The whole state goes in as JSON rather than columns: the engine owns
        the shape, and a schema that has to be migrated in step with it is a
        schema that will be one field behind on the day it matters.
        """
        payload = json.dumps(state, sort_keys=True)
        self._exec(
            "INSERT INTO carry_position(id, book_state, payload, updated_epoch) "
            "VALUES(1, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
            "book_state = excluded.book_state, payload = excluded.payload, "
            "updated_epoch = excluded.updated_epoch",
            (str(state.get("book_state", "FLAT")), payload, utc_now_epoch()),
        )

    def load_carry_position(self) -> Optional[Dict[str, Any]]:
        """What the last run believed, or None if it never wrote anything.

        None means "no carry book has ever run against this database". It does
        NOT mean flat: a flat book writes a row saying so, and the difference
        is what lets cold start tell "nothing to resume" from "the ledger was
        lost".
        """
        rows = self._query("SELECT * FROM carry_position WHERE id = 1")
        if not rows:
            return None
        raw = str(rows[0]["payload"] or "")
        if not raw:
            return None
        try:
            state = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PersistenceError(
                f"carry_position payload is not readable JSON: {exc}") from exc
        if not isinstance(state, dict):
            raise PersistenceError(
                f"carry_position payload is {type(state).__name__}, not an object")
        state["updated_epoch"] = float(rows[0]["updated_epoch"] or 0.0)
        return state

    def clear_kill_switch_by_human(self, operator_ack: str) -> None:
        """Clear the kill switch.  Requires an explicit operator acknowledgement.

        This exists so a human with shell access can resume trading.  It is
        never called from any automated code path in this codebase, and must not
        be: a kill switch the bot can reset is not a kill switch.  The required
        token is intentionally not derivable from config.
        """
        if operator_ack != "HUMAN_CLEARED_KILL_SWITCH":
            raise PersistenceError(
                "kill switch clear requires the exact operator acknowledgement; "
                "this is a human-only action"
            )
        self._exec(
            "UPDATE kill_switch SET engaged = 0, reason = '', engaged_epoch = 0.0 "
            "WHERE id = 1"
        )

    # -- per-symbol cooldown ----------------------------------------------

    def set_symbol_cooldown(self, symbol: str, seconds: float, reason: str = "") -> None:
        self._exec(
            "INSERT INTO symbol_cooldown(symbol, until_epoch, reason) VALUES(?,?,?) "
            "ON CONFLICT(symbol) DO UPDATE SET "
            "until_epoch = MAX(until_epoch, excluded.until_epoch), "
            "reason = excluded.reason",
            (symbol, utc_now_epoch() + max(0.0, float(seconds)), str(reason)),
        )

    def symbol_cooldown_remaining(self, symbol: str) -> float:
        rows = self._query(
            "SELECT until_epoch FROM symbol_cooldown WHERE symbol = ?", (symbol,)
        )
        if not rows:
            return 0.0
        return max(0.0, float(rows[0]["until_epoch"]) - utc_now_epoch())

    # -- trades ------------------------------------------------------------

    def record_trade(self, trade: TradeRecord) -> int:
        cur = self._exec(
            "INSERT INTO trades(symbol, side, qty, entry_price, exit_price, gross_pnl,"
            " entry_fee, exit_fee, net_pnl, opened_epoch, closed_epoch, day,"
            " order_link_id, meta) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                trade.symbol, trade.side, float(trade.qty), float(trade.entry_price),
                float(trade.exit_price), float(trade.gross_pnl), float(trade.entry_fee),
                float(trade.exit_fee), trade.net_pnl, float(trade.opened_epoch),
                float(trade.closed_epoch), utc_day(trade.closed_epoch),
                trade.order_link_id, json.dumps(trade.meta, default=str),
            ),
        )
        day = utc_day(trade.closed_epoch)
        self._exec(
            "UPDATE daily_anchor SET realised_pnl = realised_pnl + ?, "
            "trades = trades + 1 WHERE day = ?",
            (trade.net_pnl, day),
        )
        return int(cur.lastrowid or 0)

    def recent_trades(self, limit: int = 200) -> List[Dict[str, Any]]:
        rows = self._query(
            "SELECT * FROM trades ORDER BY closed_epoch DESC LIMIT ?", (int(limit),)
        )
        return [dict(r) for r in rows]

    def net_returns(self, limit: int = 500) -> List[float]:
        """Per-trade NET return fractions, oldest first — the Kelly input.

        Fee-inclusive by construction.  Returns are expressed against the
        trade's own notional so they are comparable across position sizes.
        """
        rows = self._query(
            "SELECT net_pnl, qty, entry_price FROM trades "
            "ORDER BY closed_epoch DESC LIMIT ?",
            (int(limit),),
        )
        out: List[float] = []
        for r in reversed(rows):
            notional = abs(float(r["qty"]) * float(r["entry_price"]))
            if notional > 0.0:
                out.append(float(r["net_pnl"]) / notional)
        return out

    def consecutive_losses(self) -> int:
        """Count of the most recent unbroken run of fee-aware losing trades."""
        rows = self._query(
            "SELECT net_pnl FROM trades ORDER BY closed_epoch DESC LIMIT 200"
        )
        n = 0
        for r in rows:
            if float(r["net_pnl"]) <= 0.0:
                n += 1
            else:
                break
        return n

    # -- positions ---------------------------------------------------------

    def upsert_position(
        self,
        symbol: str,
        side: str,
        qty: float,
        entry_price: float,
        stop_price: float = 0.0,
        order_link_id: str = "",
        meta: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self._exec(
            "INSERT INTO positions(symbol, side, qty, entry_price, stop_price,"
            " opened_epoch, order_link_id, meta) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(symbol) DO UPDATE SET side=excluded.side, qty=excluded.qty,"
            " entry_price=excluded.entry_price, stop_price=excluded.stop_price,"
            " order_link_id=excluded.order_link_id, meta=excluded.meta",
            (
                symbol, side, float(qty), float(entry_price), float(stop_price),
                utc_now_epoch(), order_link_id, json.dumps(dict(meta or {}), default=str),
            ),
        )

    def set_position_stop(self, symbol: str, stop_price: float) -> None:
        self._exec(
            "UPDATE positions SET stop_price = ? WHERE symbol = ?",
            (float(stop_price), symbol),
        )

    def remove_position(self, symbol: str) -> None:
        self._exec("DELETE FROM positions WHERE symbol = ?", (symbol,))

    def open_positions(self) -> List[Dict[str, Any]]:
        return [dict(r) for r in self._query("SELECT * FROM positions")]

    def open_position_count(self) -> int:
        rows = self._query("SELECT COUNT(*) AS n FROM positions")
        return int(rows[0]["n"])

    def positions_without_stops(self) -> List[Dict[str, Any]]:
        """Naked positions — the condition that must never persist.

        A position whose ``stop_price`` is 0 has no protective stop recorded.
        """
        return [
            dict(r)
            for r in self._query("SELECT * FROM positions WHERE stop_price <= 0.0")
        ]

    # -- orders + deterministic idempotency --------------------------------

    def next_order_seq(self) -> int:  # noqa: D401 - see docstring below
        """Monotonic, persisted order counter.

        This is what makes ``orderLinkId`` deterministic across restarts.  The
        legacy generators used ``perf_counter()`` buckets, ``uuid4``,
        ``secrets.token_hex`` and salted ``hash()`` — all of which produce a
        different id for the same intent after a restart, so a retry following a
        timed-out submit created a duplicate live order.
        """
        self._assert_writer()
        with self._lock:
            try:
                cur = self._conn.execute("SELECT next_id FROM order_seq WHERE id = 1")
                row = cur.fetchone()
                if row is None:
                    raise PersistenceError("order_seq row missing")
                n = int(row["next_id"])
                self._conn.execute(
                    "UPDATE order_seq SET next_id = ? WHERE id = 1", (n + 1,)
                )
                self._conn.commit()
                return n
            except PersistenceError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise PersistenceError(f"order sequence failed: {exc}") from exc

    def record_order(
        self,
        order_link_id: str,
        symbol: str,
        side: str,
        order_type: str,
        qty: float,
        price: float = 0.0,
        purpose: str = "entry",
        status: str = "pending",
        meta: Optional[Mapping[str, Any]] = None,
    ) -> bool:
        """Record an order intent BEFORE submitting it.

        Returns True if this is a new intent, False if the ``order_link_id`` was
        already recorded (i.e. this is a retry of something already sent).  The
        caller must not submit again on False without first querying the
        exchange for that id.
        """
        # ATOMIC claim. A SELECT-then-INSERT is a race: two threads can both
        # observe "not present", both insert, and one then dies on the UNIQUE
        # constraint — after its caller already believed it owned the id and was
        # clear to submit. `ON CONFLICT DO NOTHING` plus `rowcount` makes the
        # check and the claim a single statement, so exactly one caller wins.
        #
        # This is the guard that stops a retry from becoming a second live
        # order, so it must hold under contention, not merely in the happy path.
        now = utc_now_epoch()
        cursor = self._exec(
            "INSERT INTO orders(order_link_id, symbol, side, order_type, qty, price,"
            " purpose, status, created_epoch, updated_epoch, meta)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(order_link_id) DO NOTHING",
            (
                order_link_id, symbol, side, order_type, float(qty), float(price),
                purpose, status, now, now, json.dumps(dict(meta or {}), default=str),
            ),
        )
        return cursor.rowcount == 1

    def update_order_status(
        self, order_link_id: str, status: str, exchange_id: str = ""
    ) -> None:
        self._exec(
            "UPDATE orders SET status = ?, updated_epoch = ?, "
            "exchange_id = COALESCE(NULLIF(?, ''), exchange_id) "
            "WHERE order_link_id = ?",
            (status, utc_now_epoch(), exchange_id, order_link_id),
        )

    def get_order(self, order_link_id: str) -> Optional[Dict[str, Any]]:
        rows = self._query(
            "SELECT * FROM orders WHERE order_link_id = ?", (order_link_id,)
        )
        return dict(rows[0]) if rows else None

    def pending_orders(self) -> List[Dict[str, Any]]:
        return [
            dict(r)
            for r in self._query(
                "SELECT * FROM orders WHERE status IN ('pending','submitted','partial')"
            )
        ]

    def prune_decisions(self, *, older_than_days: int) -> int:
        """Delete journal rows older than the retention window. Returns count.

        The journal is evidence, not state: nothing reads it to decide. So a
        bounded window loses nothing a gate depends on, while an unbounded one
        grows ~1,440 rows/day/symbol under a permanently blocking gate.
        """
        cutoff = utc_now_epoch() - float(older_than_days) * 86_400.0
        cur = self._exec("DELETE FROM decisions WHERE ts_epoch < ?", (cutoff,))
        return int(cur.rowcount or 0)

    def unresolved_orders(self) -> List[Dict[str, Any]]:
        """pending_orders() plus rows whose submit outcome is 'unknown'.

        Read by startup reconciliation only. Kept separate from
        pending_orders() so that shutdown never attempts to cancel an order
        the exchange may never have received.
        """
        return [
            dict(r)
            for r in self._query(
                "SELECT * FROM orders WHERE status IN "
                "('pending','submitted','partial','unknown')"
            )
        ]

    # -- decision journal --------------------------------------------------

    def journal(
        self,
        symbol: str,
        decision: str,
        reason: str,
        detail: Optional[Mapping[str, Any]] = None,
    ) -> None:
        """Record why a decision was made.  Every block must leave a trace."""
        self._exec(
            "INSERT INTO decisions(ts_epoch, symbol, decision, reason, detail)"
            " VALUES(?,?,?,?,?)",
            (
                utc_now_epoch(), symbol, decision, reason,
                json.dumps(dict(detail or {}), default=str),
            ),
        )

    def recent_decisions(self, limit: int = 100) -> List[Dict[str, Any]]:
        return [
            dict(r)
            for r in self._query(
                "SELECT * FROM decisions ORDER BY ts_epoch DESC LIMIT ?", (int(limit),)
            )
        ]

    def decision_reason_counts(
        self,
        decisions: Optional[Sequence[str]] = ("BLOCK",),
        symbol: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Aggregate the decision journal by (symbol, decision, reason).

        Aggregating in SQL rather than in Python keeps the whole journal out of
        the health thread's memory; the journal is append-only and unbounded,
        and a health endpoint that has to materialise it will eventually be the
        thing that kills the process.

        ``decisions=None`` aggregates every verdict.  An empty sequence matches
        nothing and returns ``[]`` rather than producing ``IN ()``, which is a
        syntax error rather than an empty result.
        """
        clauses: List[str] = []
        params: List[Any] = []
        if decisions is not None:
            wanted = [str(d).upper() for d in decisions]
            if not wanted:
                return []
            clauses.append(
                "UPPER(decision) IN (%s)" % ",".join("?" for _ in wanted)
            )
            params.extend(wanted)
        if symbol:
            clauses.append("symbol = ?")
            params.append(str(symbol))
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self._query(
            "SELECT symbol, decision, reason, COUNT(*) AS n, "
            "MAX(ts_epoch) AS last_epoch FROM decisions "
            f"{where} GROUP BY symbol, decision, reason ORDER BY n DESC, reason ASC",
            tuple(params),
        )
        return [dict(r) for r in rows]

    # -- execution quality -------------------------------------------------

    def record_execution(
        self,
        symbol: str,
        side: str,
        intended_price: float,
        fill_price: float = 0.0,
        qty: float = 0.0,
        order_link_id: str = "",
        order_type: str = "",
        purpose: str = "entry",
        outcome: str = "filled",
        reason: str = "",
        is_maker: bool = False,
        requested_epoch: Optional[float] = None,
        filled_epoch: Optional[float] = None,
        meta: Optional[Mapping[str, Any]] = None,
    ) -> int:
        """Record what an order actually cost, against what it was meant to cost.

        This is the single most valuable thing the bot can remember, because it
        is the only measurement that can falsify the cost model.  ``MIN_EDGE_BPS``
        is derived from an *assumed* round-trip cost; if real slippage is twice
        the assumption, every edge calculation in the stack is optimistic by the
        difference and no other table can reveal that.

        ``slippage_bps`` is computed here, once, by :func:`slippage_bps`, so the
        sign convention cannot diverge between call sites.  It is stored as NULL
        — not 0.0 — whenever it cannot be measured (a rejected order has no
        fill), because a fabricated zero would drag the mean toward "no cost".

        ``time_to_fill_s`` likewise stays NULL unless both epochs are supplied
        and ordered; a negative duration is a clock problem, not a fast fill.

        Returns the row id.
        """
        out = str(outcome).strip().lower()
        if out not in EXECUTION_OUTCOMES:
            raise PersistenceError(
                f"unknown execution outcome {outcome!r}; expected one of "
                f"{EXECUTION_OUTCOMES}. An unrecognised category is one nobody "
                "aggregates, which is the same as not recording it."
            )
        norm_side = str(side).strip().lower()
        if norm_side not in ("buy", "sell", "long", "short"):
            raise PersistenceError(
                f"unknown side {side!r}: slippage has no sign without a side"
            )

        slip: Optional[float] = None
        if out in ("filled", "partial"):
            try:
                slip = slippage_bps(side, intended_price, fill_price)
            except PersistenceError:
                # An unmeasurable fill is recorded WITHOUT a slippage figure
                # rather than refused: losing the fact that the fill happened is
                # worse than losing one cost measurement.
                slip = None

        ttf: Optional[float] = None
        if requested_epoch is not None and filled_epoch is not None:
            delta = float(filled_epoch) - float(requested_epoch)
            if delta == delta and delta >= 0.0:  # finite and non-negative
                ttf = delta

        ts = float(filled_epoch) if filled_epoch is not None else utc_now_epoch()
        if ts != ts:  # NaN
            ts = utc_now_epoch()

        cur = self._exec(
            "INSERT INTO execution_quality(ts_epoch, symbol, order_link_id, side,"
            " order_type, purpose, outcome, reason, intended_price, fill_price,"
            " qty, slippage_bps, is_maker, time_to_fill_s, meta)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                ts, str(symbol), str(order_link_id), str(side), str(order_type),
                str(purpose), out, str(reason), float(intended_price),
                float(fill_price), float(qty), slip, 1 if is_maker else 0, ttf,
                json.dumps(dict(meta or {}), default=str),
            ),
        )
        return int(cur.lastrowid or 0)

    def recent_executions(
        self, limit: int = 500, symbol: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Most recent execution records first.  A read — no writer role needed."""
        if symbol:
            rows = self._query(
                "SELECT * FROM execution_quality WHERE symbol = ? "
                "ORDER BY ts_epoch DESC, id DESC LIMIT ?",
                (str(symbol), int(limit)),
            )
        else:
            rows = self._query(
                "SELECT * FROM execution_quality ORDER BY ts_epoch DESC, id DESC "
                "LIMIT ?",
                (int(limit),),
            )
        return [dict(r) for r in rows]

    # -- regime history ----------------------------------------------------

    def record_regime(
        self,
        symbol: str,
        regime: str,
        ts_epoch: Optional[float] = None,
        detail: Optional[Mapping[str, Any]] = None,
    ) -> int:
        """Record the regime observed for ``symbol`` at a moment in time.

        Observations only.  What the trades opened under this regime went on to
        do is derived from ``trades`` by :meth:`trades_with_regime`, so there is
        no outcome column here to drift from the ledger.
        """
        label = str(regime).strip()
        if not label:
            raise PersistenceError("refusing to record an empty regime label")
        ts = utc_now_epoch() if ts_epoch is None else float(ts_epoch)
        if ts != ts or ts in (float("inf"), float("-inf")):
            raise PersistenceError(f"non-finite regime timestamp {ts_epoch!r}")
        cur = self._exec(
            "INSERT INTO regime_history(ts_epoch, symbol, regime, detail)"
            " VALUES(?,?,?,?)",
            (ts, str(symbol), label, json.dumps(dict(detail or {}), default=str)),
        )
        return int(cur.lastrowid or 0)

    def recent_regimes(
        self, limit: int = 200, symbol: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        if symbol:
            rows = self._query(
                "SELECT * FROM regime_history WHERE symbol = ? "
                "ORDER BY ts_epoch DESC, id DESC LIMIT ?",
                (str(symbol), int(limit)),
            )
        else:
            rows = self._query(
                "SELECT * FROM regime_history ORDER BY ts_epoch DESC, id DESC LIMIT ?",
                (int(limit),),
            )
        return [dict(r) for r in rows]

    def current_regime(
        self, symbol: str, at_epoch: Optional[float] = None
    ) -> Optional[str]:
        """The regime in force for ``symbol`` at ``at_epoch`` (default: now).

        ``None`` when nothing was ever recorded for that symbol at or before
        that instant — never a default label, which would let an unobserved
        symbol inherit another symbol's history.
        """
        when = utc_now_epoch() if at_epoch is None else float(at_epoch)
        rows = self._query(
            "SELECT regime FROM regime_history WHERE symbol = ? AND ts_epoch <= ? "
            "ORDER BY ts_epoch DESC, id DESC LIMIT 1",
            (str(symbol), when),
        )
        return str(rows[0]["regime"]) if rows else None

    def trades_with_regime(self, limit: int = 500) -> List[Dict[str, Any]]:
        """Closed trades, each labelled with the regime in force when it OPENED.

        Opened, not closed: the question the operator is asking is "given what
        the market looked like when I decided, how did that decision turn out".
        Labelling by close time would attribute the outcome to a regime that had
        not yet been observed when the risk was taken.

        ``regime`` is NULL for trades opened before any observation for that
        symbol.  Those trades are not guessed at; they are simply unlabelled.
        """
        rows = self._query(
            "SELECT t.*, ("
            "  SELECT r.regime FROM regime_history r"
            "   WHERE r.symbol = t.symbol AND r.ts_epoch <= t.opened_epoch"
            "   ORDER BY r.ts_epoch DESC, r.id DESC LIMIT 1"
            ") AS regime "
            "FROM trades t ORDER BY t.closed_epoch DESC LIMIT ?",
            (int(limit),),
        )
        return [dict(r) for r in rows]

    # -- namespaced key/value memory ---------------------------------------

    def memory_put(self, namespace: str, key: str, value: Any) -> None:
        """Store a JSON-serialisable fact under ``namespace``/``key``.

        See :data:`MEMORY_KEY_CONVENTION`.  The value is serialised eagerly so a
        value that cannot round-trip is rejected at the write, where the caller
        can still do something about it, rather than at the read.
        """
        ns, k = str(namespace).strip(), str(key).strip()
        if not ns or not k:
            raise PersistenceError("memory namespace and key must both be non-empty")
        try:
            payload = json.dumps(value)
        except (TypeError, ValueError) as exc:
            raise PersistenceError(
                f"memory value for {ns}/{k} is not JSON-serialisable: {exc}"
            ) from exc
        self._exec(
            "INSERT INTO memory_kv(namespace, key, value, updated_epoch)"
            " VALUES(?,?,?,?) ON CONFLICT(namespace, key) DO UPDATE SET"
            " value = excluded.value, updated_epoch = excluded.updated_epoch",
            (ns, k, payload, utc_now_epoch()),
        )

    def memory_get(self, namespace: str, key: str, default: Any = None) -> Any:
        rows = self._query(
            "SELECT value FROM memory_kv WHERE namespace = ? AND key = ?",
            (str(namespace), str(key)),
        )
        if not rows:
            return default
        try:
            return json.loads(rows[0]["value"])
        except (TypeError, ValueError) as exc:
            raise PersistenceError(
                f"stored memory value for {namespace}/{key} is not valid JSON: {exc}"
            ) from exc

    def memory_namespace(self, namespace: str) -> Dict[str, Any]:
        """Every key in one namespace, decoded.  Undecodable values are skipped
        rather than raised, so one poisoned key cannot blind the whole namespace."""
        out: Dict[str, Any] = {}
        for row in self._query(
            "SELECT key, value FROM memory_kv WHERE namespace = ? ORDER BY key",
            (str(namespace),),
        ):
            try:
                out[str(row["key"])] = json.loads(row["value"])
            except (TypeError, ValueError):
                continue
        return out

    def memory_namespaces(self) -> List[str]:
        return [
            str(r["namespace"])
            for r in self._query(
                "SELECT DISTINCT namespace FROM memory_kv ORDER BY namespace"
            )
        ]

    def memory_forget(self, namespace: str, key: str) -> bool:
        """Delete one key.  True if something was actually removed."""
        cur = self._exec(
            "DELETE FROM memory_kv WHERE namespace = ? AND key = ?",
            (str(namespace), str(key)),
        )
        return cur.rowcount > 0

    # -- reads the memory layer needs --------------------------------------

    def trades_for_symbol(self, symbol: str, limit: int = 500) -> List[Dict[str, Any]]:
        """Most recent closed trades for one symbol, newest first."""
        return [
            dict(r)
            for r in self._query(
                "SELECT * FROM trades WHERE symbol = ? "
                "ORDER BY closed_epoch DESC LIMIT ?",
                (str(symbol), int(limit)),
            )
        ]

    def traded_symbols(self) -> List[str]:
        return [
            str(r["symbol"])
            for r in self._query("SELECT DISTINCT symbol FROM trades ORDER BY symbol")
        ]
