"""Structured record of a paper trading session.

WHAT THIS IS
============
One row in, one row out: what the machine was configured to do, whether it was
armed, whether the kill switch was engaged, how many ticks it ran, and how many
orders actually left the process. That is the whole record.

WHAT THIS DELIBERATELY IS NOT
=============================
**It is not a performance report.** There is no PnL field, no return, no win
rate, no equity curve, and there is a test that fails if one appears — over both
the field set and this module's own source.

The reason is specific, not stylistic. Timing-skill research on this codebase is
CLOSED with an ABSENT verdict for both signal families measured
(`RESEARCH_CLOSE_STAGE1.md`): the classical analyser scored M1/M2 76.1/77.5 and
`donchian_breakout_v1` scored 91.2/91.5, against a pre-declared bar of 95.0. A
session log that reported profit would invite exactly the inference the close-out
forbids — reading a number produced by a positive-drift asset as evidence of
skill. Roughly half the strategy's headline expectancy is reproducible by an
entry schedule that cannot see prices at all.

So every record carries :data:`NO_EDGE_CLAIM`, verbatim and unmodified, and the
record carries no profit figure to misread.

WHAT IT MUST NEVER CONTAIN
==========================
Credentials. No API key, no secret, no signature, in any field, at any time. The
recorder never reads them, and a test places a known key and secret in config and
asserts neither appears anywhere in the record or the file.

WHERE IT GOES
=============
Two places, neither of them a new datastore:

* ``SESSION_START`` and ``SESSION_END`` rows in the existing ``decisions``
  journal via ``StateStore.journal``, under the symbol ``"SESSION"``. It
  inherits single-writer discipline and restart persistence for free.
* optionally, one JSON object per line appended to ``PAPER_SESSION_LOG_PATH``
  for an operator who wants to ``tail`` something.

**Logging can never take the bot down.** Every write is wrapped: an unwritable
path, a full disk or a closed store is logged at WARNING and the session
continues. A telemetry failure that halts a trading process is a worse bug than
the missing telemetry.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

__all__ = ["PaperSessionLog", "NO_EDGE_CLAIM", "SCHEMA", "utc_iso"]

#: The schema tag. Bump it if a field's meaning changes, so an old record is
#: never silently read under new rules.
SCHEMA = "paper_session/1"

#: Mandatory, constant, and asserted by test. This is the sentence that stops a
#: clean paper run from being quoted as evidence of an edge that measurement has
#: twice failed to find.
NO_EDGE_CLAIM = "NO EDGE CLAIM — timing-skill research CLOSED"

#: Substrings that must never appear in a FIELD NAME. Checked two ways in
#: `tests/test_paper_session_log.py`: against the keys of a live record (which
#: catches a key added through `.update()`), and against the string keys of the
#: dict literal in `PaperSessionLog.record` read via AST (which catches one added
#: to the source even if that branch never ran in a test).
#:
#: The check is on field NAMES, not on this module's source text — `return` is a
#: Python keyword and `skill` appears in the docstring above precisely to explain
#: why none of this is here.
#:
#: The failure mode being guarded against is a helpful future edit — "operators
#: will want to see how it did" — not malice.
FORBIDDEN_FIELD_MARKERS = (
    "pnl", "profit", "return", "win_rate", "winrate", "equity",
    "performance", "sharpe", "expectancy", "drawdown",
)


def _research_state() -> str:
    """The project's research state, or a fail-closed default.

    Imported inside the function: `project_status` imports `NO_EDGE_CLAIM` from
    this module, so a module-level import would be circular. If the import fails
    for any reason the answer is still "CLOSED" — the safe direction, because a
    session record that omitted the closure would read as though research were
    open.
    """
    try:
        import project_status
        return project_status.RESEARCH_CLOSED
    except Exception:  # noqa: BLE001
        return "CLOSED"


def _cleared_edge_signal() -> Optional[str]:
    """The cleared-edge signal, or None. Fail closed to None."""
    try:
        import project_status
        return project_status.current(None).cleared_edge_signal
    except Exception:  # noqa: BLE001
        return None


def _research_clear() -> Optional[dict]:
    """A description of the cleared research claim, or None.

    Added slice 58. The session record already carried the cleared signal's
    NAME; a name on its own invites a reader to assume more behind it than
    there is. This block carries the shape of the evidence with it — the
    window, the trade count, and an explicit statement that a shadow fill is
    not a live claim — so an operator sees what cleared and how thin it was in
    the same glance.

    Deliberately carries NO PnL-shaped field. `FORBIDDEN_FIELD_MARKERS` refuses
    one and a structural test asserts it over this module; a shadow pilot that
    started reporting an equity curve would be a production profit claim
    wearing a research label.
    """
    try:
        import project_status
        cleared = project_status.current(None).cleared_edge_signal
    except Exception:  # noqa: BLE001
        return None
    if not cleared:
        return None
    block = {
        "signal": cleared,
        "stage": "research_clear",
        "live_claim": False,
        "note": ("Research has cleared this product's pre-declared Stage-1 "
                 "gate. It is not a live claim, nothing is armed, and a "
                 "simulated fill is not evidence of skill."),
    }
    try:
        import json
        import os
        repo = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(repo, "artifacts",
                            "slice57_oos_edge_BTCUSDT_summary.json")
        with open(path, encoding="utf-8") as handle:
            summary = json.load(handle)
        if summary.get("signal") == cleared:
            block["window"] = summary.get("window")
            block["scored_trades"] = summary.get("observed", {}).get("n_trades")
            block["m1"] = round(summary.get("m1", {}).get("percentile", 0.0), 2)
            block["m2"] = round(summary.get("m2", {}).get("percentile", 0.0), 2)
    except Exception:  # noqa: BLE001
        # Fail closed to the name alone rather than to a guess.
        pass
    return block


def utc_iso(epoch: Optional[float] = None) -> str:
    """ISO-8601 in UTC with a trailing Z. Never local time."""
    moment = datetime.fromtimestamp(
        time.time() if epoch is None else float(epoch), tz=timezone.utc,
    )
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class PaperSessionLog:
    """Records one paper session. Construct at startup, ``close()`` at shutdown.

    Nothing here reaches into the trading path. It reads state that already
    exists — config flags, the kill-switch row, the client's own submission
    counter — and writes it down.
    """

    def __init__(
        self,
        *,
        config: Any,
        store: Any = None,
        client: Any = None,
        path: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> None:
        self.cfg = config
        self.store = store
        self.client = client
        self.session_id = session_id or uuid.uuid4().hex
        self.started_epoch = time.time()
        self.ended_epoch: Optional[float] = None
        self.ticks = 0
        self.reconciled = False
        self.path = path if path is not None else str(
            getattr(config, "PAPER_SESSION_LOG_PATH", "") or "")

    # -- the record ---------------------------------------------------------

    def _kill_switch(self) -> Dict[str, Any]:
        """Fail closed: if the switch cannot be read, report it as engaged.

        An unreadable kill switch is treated as engaged everywhere else in this
        codebase (`risk_management.kill_active`). A session log that reported
        `false` because the read threw would be the one place that disagreed.
        """
        if self.store is None:
            return {"kill_switch_engaged": False, "kill_switch_reason": ""}
        try:
            engaged, reason = self.store.is_kill_switch_engaged()
            return {"kill_switch_engaged": bool(engaged),
                    "kill_switch_reason": str(reason or "")}
        except Exception as exc:  # noqa: BLE001
            logger.warning("kill switch unreadable for the session log: %s", exc)
            return {"kill_switch_engaged": True,
                    "kill_switch_reason": "UNREADABLE"}

    def record(self) -> Dict[str, Any]:
        """The session as a plain dict. No PnL. No credentials. Ever."""
        cfg = self.cfg
        record: Dict[str, Any] = {
            "schema": SCHEMA,
            "session_id": self.session_id,
            "started_utc": utc_iso(self.started_epoch),
            "ended_utc": utc_iso(self.ended_epoch) if self.ended_epoch else None,
            "entries_enabled": bool(getattr(cfg, "ENTRIES_ENABLED", True)),
            "paper": bool(getattr(cfg, "PAPER_TRADING", True)),
            "testnet": bool(getattr(cfg, "USE_TESTNET", True)),
            "live_authorized": bool(getattr(cfg, "LIVE_AUTHORIZED", False)),
            "policy_mode": str(getattr(cfg, "POLICY_MODE", "off")),
            "model_loaded": False,
            "ticks": int(self.ticks),
            "orders_submitted": int(getattr(self.client, "orders_submitted", 0)),
            "reconciled": bool(self.reconciled),
            # What mode produced this session. Imported lazily so `session_log`
            # stays importable on its own — `project_status` imports the claim
            # constant from here, and a module-level import would be circular.
            "timing_skill_research": _research_state(),
            "cleared_edge_signal": _cleared_edge_signal(),
            "research_clear": _research_clear(),
            "no_edge_claim": NO_EDGE_CLAIM,
        }
        record.update(self._kill_switch())
        return record

    # -- writing ------------------------------------------------------------

    def _journal(self, event: str) -> None:
        if self.store is None:
            return
        try:
            self.store.journal("SESSION", event, NO_EDGE_CLAIM, self.record())
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not journal %s: %s", event, exc)

    def _append_file(self, event: str) -> None:
        if not self.path:
            return
        try:
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            payload = {"event": event, **self.record()}
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, default=str) + "\n")
        except Exception as exc:  # noqa: BLE001
            # Deliberately swallowed. A session log that can halt a trading
            # process is a worse bug than a session log that is missing.
            logger.warning("could not write the session log to %s: %s",
                           self.path, exc)

    def _emit(self, event: str) -> None:
        self._journal(event)
        self._append_file(event)

    # -- lifecycle ----------------------------------------------------------

    def start(self, *, reconciled: bool = False) -> Dict[str, Any]:
        self.reconciled = bool(reconciled)
        record = self.record()
        logger.warning(
            "paper session %s start | entries_enabled=%s paper=%s testnet=%s "
            "live_authorized=%s | %s",
            self.session_id, record["entries_enabled"], record["paper"],
            record["testnet"], record["live_authorized"], NO_EDGE_CLAIM,
        )
        self._emit("SESSION_START")
        return record

    def tick(self) -> None:
        self.ticks += 1

    def close(self) -> Dict[str, Any]:
        """End the session. Idempotent: a session ends exactly once.

        `TradingBot.shutdown()` calls this, and an operator harness may have
        called it already. Without the guard the record is emitted twice and a
        reader counting `SESSION_END` rows would over-count sessions.
        """
        if self.ended_epoch is not None:
            return self.record()
        self.ended_epoch = time.time()
        record = self.record()
        logger.warning(
            "paper session %s end | ticks=%d orders_submitted=%d "
            "kill_switch=%s | %s",
            self.session_id, record["ticks"], record["orders_submitted"],
            record["kill_switch_engaged"], NO_EDGE_CLAIM,
        )
        self._emit("SESSION_END")
        return record
