"""main.py — one orchestrator.

WHAT REPLACED WHAT
==================
The as-received module (7,801 lines, retained at ``_dead/main_legacy.py``)
contained six orchestrator classes, of which ``main()`` ran exactly one. It also
had a structural defect that made most of the file unreachable regardless of
intent: ``if __name__ == "__main__": main()`` sat at line 6204, and ``main()``
blocks. Everything defined below it — five orchestrator classes,
``run_autonomous_trading_loop``, ``wire_p2_integrations`` — did not exist yet
when ``main()`` ran.

Also removed here:

* the ``os.getenv`` monkey-patch installed process-wide at import (line 58);
* ``PAPER_TRADING = False`` hardcoded under a comment claiming "Paper trading ON
  by default";
* ``USE_TESTNET`` parsed as ``== "true"``, so the Dockerfile's safety default of
  ``USE_TESTNET=1`` silently meant **mainnet**;
* the contrarian mode that flipped strong SELL signals into BUYs;
* the EMA fallback that fabricated BUY signals at confidence 0.66 when the
  strategy returned HOLD;
* the hardcoded ``90% of equity x leverage`` sizing at line 4008;
* the ``confidence > 0.05`` execution gate.

Startup order is fixed and non-negotiable:

    config -> state store -> client -> risk -> sizer -> engine
    -> RECONCILE -> health server -> trading loop

Reconciliation happens before the first trade of every run, because the legacy
bot's most dangerous behaviour was restarting with amnesia and stacking new
positions on top of forgotten ones.
"""

from __future__ import annotations

import json
import logging
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
import datetime as dt
from typing import Any, Callable, Dict, List, Optional, Tuple

import config as _config
from bybit_connection import BybitAPIError, BybitClient
from memory import TradingMemory
from persistence import StateStore
import project_status as _project_status
from session_log import PaperSessionLog
from position_sizing import BillionairePositionSizing
from risk_management import BillionaireRiskManager
from trading_engine import TradeIntent, TradingEngine

logger = logging.getLogger("main")

__all__ = ["TradingBot", "build_bot", "main"]

#: Safety margin added on top of `private_ws_consumer.RECV_TIMEOUT_SECONDS`
#: when joining the WS thread at shutdown — see
#: `TradingBot._stop_private_ws()`. The receive loop's own blocking
#: `recv()` call is the longest interval between the WS thread checking
#: `ws_stop_event`; the join budget must exceed that, not guess a fixed
#: number smaller than it.
WS_SHUTDOWN_SAFETY_MARGIN_SECONDS = 5.0

#: Consecutive ticks the carry engine may raise while a pair is HELD before the
#: book is halted. A held short that no tick can manage is an unmanaged
#: exposure; a raise that repeats is a defect, not a transient read (the
#: engine's own venue reads halt or exit on their own failures). Not a trading
#: parameter: it bounds how long an unmanageable book is retried silently.
CARRY_MAX_CONSECUTIVE_HELD_ERRORS = 3


# ---------------------------------------------------------------------------
# health server
# ---------------------------------------------------------------------------


class _HealthHandler(BaseHTTPRequestHandler):
    """Answers the container health check with real state.

    Audit C18: `ENABLE_HEALTH_CHECK=true` was configured but **no health server
    existed**. If the orchestrator health-checks the port, the task is killed and
    restarted in a loop — and each restart re-entered live trading with amnesia.
    """

    #: A client that connects and says nothing must not hold the server (and
    #: with it every later check) for ever.
    timeout = 5

    bot: Optional["TradingBot"] = None

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        try:
            payload = self.bot.health() if self.bot else {"status": "starting"}
            healthy = bool(payload.get("healthy"))
        except Exception as exc:  # noqa: BLE001
            payload, healthy = {"healthy": False, "error": str(exc)}, False
        body = json.dumps(payload).encode()
        self.send_response(200 if healthy else 503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_a: Any) -> None:
        """Silence per-request logging; health checks are frequent and noisy."""


# ---------------------------------------------------------------------------
# the orchestrator
# ---------------------------------------------------------------------------


class TradingBot:
    """The one orchestrator. Owns startup, the loop, and shutdown."""

    def __init__(
        self,
        config: Any = None,
        store: Optional[StateStore] = None,
        client: Optional[BybitClient] = None,
        risk_manager: Optional[BillionaireRiskManager] = None,
        engine: Optional[TradingEngine] = None,
        strategy: Optional[Any] = None,
        carry: Optional[Any] = None,
        ws_transport_factory: Optional[Callable[[], Any]] = None,
        ws_evidence_path: Optional[str] = None,
        ws_enabled_override: Optional[bool] = None,
        ws_assurance_mode: bool = False,
    ) -> None:
        #: Injectable seam for the private-WS transport, exactly like
        #: `client`/`engine`/`strategy` above — production leaves this
        #: `None` (a real `ws_transport.PrivateWebSocket` is built from the
        #: real client's `ws_private_url`), tests inject a scripted fake so
        #: the REAL `TradingBot`/`TradingEngine`/`BybitClient`/`StateStore`
        #: can be driven through the actual application lifecycle without a
        #: socket. See `_start_private_ws`.
        self._ws_transport_factory = ws_transport_factory
        #: Same idea for the WS evidence log path: `None` (production)
        #: reads `cfg.WS_EVIDENCE_PATH`/falls back to
        #: `private_ws_consumer.DEFAULT_WS_EVIDENCE_PATH`; tests pass an
        #: explicit `tmp_path`-based file so they never write into the
        #: real repository's `artifacts/` directory as a side effect.
        self._ws_evidence_path = ws_evidence_path
        #: `None` (production): the config flag `PRIVATE_WS_ENABLED`
        #: decides, unchanged. `True`/`False`: overrides it explicitly —
        #: used by tools/testnet_conformance_run.py, which needs WS
        #: started as part of its bounded run regardless of the general
        #: trading config, without requiring an extra environment
        #: variable beyond what the conformance preflight already checks.
        self._ws_enabled_override = ws_enabled_override
        #: Passed straight through to WSPrivateConsumer — see
        #: private_ws_consumer.py's "EVIDENCE FAILURE SEMANTICS". False
        #: (NORMAL) for real trading; the conformance runner passes True.
        self._ws_assurance_mode = ws_assurance_mode
        self.ws_consumer: Optional[Any] = None
        self.ws_thread: Optional[threading.Thread] = None
        self.ws_stop_event = threading.Event()
        #: True only if `_stop_private_ws()` ever joined its timeout
        #: budget with the thread still alive — a real shutdown failure,
        #: not the ordinary case. See `_stop_private_ws()`.
        self.ws_shutdown_failed = False
        #: "DISABLED" (PRIVATE_WS_ENABLED is false, the default — REST-only
        #: observation, unchanged from before this attribute existed),
        #: "STARTING", "HEALTHY" (a WS-flagged gap was cleared by a
        #: verified REST reconciliation this cycle), or "DEGRADED" (a gap
        #: exists and has NOT yet been REST-verified, or the WS thread
        #: died). Never set to "HEALTHY" merely because a tick ran — see
        #: `_absorb_ws_observations`.
        self._observation_state = "DISABLED"
        #: The delta-neutral carry book. When present it OWNS the cycle and the
        #: directional strategy is not consulted at all — see `tick`. The two
        #: are mutually exclusive by construction, not by convention, because a
        #: process running both would hold a hedged pair AND a directional bet
        #: against the same margin.
        self.carry = carry
        #: HORIZON for the shadow book, in DAILY BARS. `None` for every other
        #: mode. ROADMAP Stage A item 2: the cleared rule holds for at most
        #: HORIZON bars and the runtime had no time exit at all — `HORIZON`
        #: appeared only in the signal module and in research tools. A pilot
        #: that holds to its stop or its target and never times out is not the
        #: rule that was measured, it is a cousin of it, which is the same
        #: class of error slice 58 found when the shadow entered mid-run.
        self.shadow_horizon_days: Optional[int] = None
        #: The double-entry journal (0044). Attached by build_bot for a carry
        #: book and None for every other mode. Fills book themselves through
        #: LedgerBroker; tick() posts funding and writes the journal down.
        self.carry_journal = None
        #: Funding already posted for the OPEN position, so a tick books only
        #: what is new. Reset when the position closes, because the next one
        #: starts its own accrual at zero.
        #: The ENGINE's running funding estimate for the open hedge. Compared
        #: with what the venue paid; never booked.
        self._carry_estimated_funding = 0.0
        self._last_funding_poll_ms = 0
        self.cfg = config if config is not None else _config.get_config_object()
        if store is None:
            # The configured path, not an import-time environment read: a
            # config built from an explicit mapping must not silently open a
            # different database (and with it a cleared kill switch and an
            # unbounded daily-loss anchor).
            db_path = getattr(self.cfg, "STATE_DB_PATH", None)
            # A live-armed process must not mistake a wiped or missing state
            # file for a first run: a new database then starts engaged.
            armed = getattr(self.cfg, "LIVE_AUTHORIZED", False) is True
            store = (StateStore(db_path.strip(), engage_if_new=armed)
                     if isinstance(db_path, str) and db_path.strip()
                     else StateStore(engage_if_new=armed))
        self.store = store
        self.client = client if client is not None else BybitClient(
            config=self.cfg, store=self.store
        )
        self.risk = risk_manager if risk_manager is not None else BillionaireRiskManager(
            config=self.cfg, store=self.store
        )
        self.sizer = BillionairePositionSizing(
            config=self.cfg, risk_manager=self.risk
        )
        #: The shared reference layer, constructed ONCE here and handed to
        #: everything that needs it. Two TradingMemory objects over one store
        #: would not corrupt anything — it is stateless — but a single instance
        #: makes it obvious in the constructor that strategy, engine and health
        #: are all looking at the same remembered history.
        self.memory: Optional[TradingMemory] = None
        if bool(getattr(self.cfg, "MEMORY_ENABLED", True)):
            try:
                self.memory = TradingMemory(self.store, self.cfg)
            except Exception:  # noqa: BLE001
                logger.warning("memory layer unavailable; continuing without it")
        self.engine = engine if engine is not None else TradingEngine(
            client=self.client, risk_manager=self.risk,
            position_sizer=self.sizer, store=self.store, config=self.cfg,
            memory=self.memory,
        )
        #: Signal source. Left injectable and OPTIONAL: with no strategy the bot
        #: runs its loop and takes no trades, which is the correct behaviour for
        #: a bot whose strategy layer is not yet repaired. The legacy code
        #: fabricated BUY signals in this situation.
        self.strategy = strategy

        self.symbols: List[str] = list(getattr(self.cfg, "TRADING_SYMBOLS", ()))
        self.loop_interval_s = float(getattr(self.cfg, "LOOP_INTERVAL_SECONDS", 60))
        self._stop = threading.Event()
        self._http: Optional[HTTPServer] = None
        self._reconciled = False
        self.started_epoch = 0.0

        #: Structured record of this session. It reports configuration, arming
        #: state, kill-switch state, tick count and the number of orders that
        #: actually left the process — and deliberately no PnL, because a paper
        #: session that reports profit invites reading profit as skill, which
        #: measurement has twice refused to support. See `session_log.py`.
        self.session = PaperSessionLog(
            config=self.cfg, store=self.store, client=self.client,
        )

    # -- health ------------------------------------------------------------

    @property
    def observation_status(self) -> str:
        """"DISABLED" / "STARTING" / "HEALTHY" / "DEGRADED", live-computed
        rather than a value that only changes inside `tick()`.

        `_observation_state` (set by `_start_private_ws`/
        `_absorb_ws_observations`) tracks the DISABLED/STARTING/failed-init
        cases and the "verified HEALTHY as of the last cycle" reading. But
        `self.ws_consumer.needs_reconciliation` can flip True the instant a
        WS event arrives, on the WS thread, between ticks — a health check
        (or a test) reading a stale stored "HEALTHY" in that window would
        be exactly the false-healthy report Section 5 exists to prevent.
        So DEGRADED is always computed live from the consumer's own
        current flag and thread liveness; only the positive (HEALTHY)
        reading is allowed to come from the last verified cycle.
        """
        consumer = self.ws_consumer
        if consumer is None:
            return self._observation_state
        if consumer.needs_reconciliation:
            return "DEGRADED"
        if self.ws_thread is not None and not self.ws_thread.is_alive():
            return "DEGRADED"
        return self._observation_state

    def health(self) -> Dict[str, Any]:
        """Real state, not a hardcoded 200.

        Reports unhealthy when the kill switch is engaged or when any position
        is unprotected — both are conditions an operator must see.
        """
        try:
            kill_engaged, kill_reason = self.store.is_kill_switch_engaged()
            naked = [p["symbol"] for p in self.store.positions_without_stops()]
            carry = self.carry_status()
            carry_blocked = bool(carry.get("blocking_fault"))
            loop_stalled = self._loop_stalled()
            unpriced = self.store.unpriced_exit_status()
            return {
                "healthy": (not kill_engaged and not naked and self._reconciled
                            and not carry_blocked and not loop_stalled
                            and not unpriced["stuck"]),
                "unpriced_exits": unpriced,
                "partially_open_after_exit": self.store.partial_open_lifecycles(),
                "funding_unbooked": self.store.funding_unbooked_summary(),
                "loop_stalled": loop_stalled,
                "consecutive_tick_failures": self._tick_failures,
                "last_tick_ok_age_s": (None if not self._last_tick_ok_epoch else
                                       round(time.time() - self._last_tick_ok_epoch, 1)),
                "risk_halted": self._risk_halted(),
                "carry": carry,
                "reconciled": self._reconciled,
                "kill_switch": kill_engaged,
                "kill_reason": kill_reason,
                "naked_positions": naked,
                "open_positions": self.store.open_position_count(),
                "paper": bool(getattr(self.cfg, "PAPER_TRADING", True)),
                "testnet": bool(getattr(self.cfg, "USE_TESTNET", True)),
                "uptime_s": round(time.time() - self.started_epoch, 1)
                if self.started_epoch else 0.0,
                "category": str(getattr(self.cfg, "CATEGORY", "spot")),
                "shorts_available": bool(getattr(self.cfg, "ALLOW_SHORTS", False)),
                "project": _project_status.current(self.cfg).as_dict(),
                "memory": self._memory_health(),
                "policy": self._policy_health(),
                "observation_status": self.observation_status,
            }
        except Exception as exc:  # noqa: BLE001
            return {"healthy": False, "error": f"{type(exc).__name__}: {exc}"}

    #: Class-level defaults so a bot built without __init__ (tests, tools) works.
    _last_tick_ok_epoch = 0.0
    _tick_failures = 0
    #: A loop is called stalled after this many consecutive failed cycles, or when
    #: no cycle has completed for this many loop intervals (and at least 120 s).
    HEALTH_MAX_TICK_FAILURES = 5
    HEALTH_STALL_INTERVALS = 3

    def _loop_stalled(self) -> bool:
        """True when the trading loop is failing or has stopped completing.

        A health endpoint that stays green while ``tick()`` raises on every cycle
        (or hangs on a venue call) tells an operator nothing."""
        if self._tick_failures >= self.HEALTH_MAX_TICK_FAILURES:
            return True
        last = self._last_tick_ok_epoch
        if not last:
            return False                      # first cycle not finished yet
        limit = max(120.0, self.HEALTH_STALL_INTERVALS * float(
            getattr(self, "loop_interval_s", 60.0) or 60.0))
        return (time.time() - last) > limit

    def _risk_halted(self) -> bool:
        try:
            return bool(self.risk.should_halt_trading())
        except Exception:  # noqa: BLE001
            return True

    def carry_status(self) -> Dict[str, Any]:
        """What an operator must know about the carry book, in one place.

        Read-only and credential-free (health is served from another thread).
        Every figure here is either what the venue last said, what the book
        recorded, or absent; none is inferred. ``blocking_fault`` is non-empty
        exactly when the book will not take new exposure.
        """
        engine = getattr(self, "carry", None)
        if engine is None:
            return {"attached": False}
        try:
            status: Dict[str, Any] = {
                "attached": True,
                "environment": {
                    "testnet": bool(getattr(self.cfg, "USE_TESTNET", True)),
                    "paper": bool(getattr(self.cfg, "PAPER_TRADING", True)),
                    "execution_mode": str(getattr(engine, "execution_mode", "")),
                },
                "book_state": engine.state.value,
                "last_decision": dict(self._last_carry_decision),
                # The carry book installs no protective stop BY DESIGN: it is
                # delta-neutral and its protection is the margin-headroom and
                # liquidation-distance exits run on every tick.
                "protective_stop": "NOT_APPLICABLE_DELTA_NEUTRAL",
                "journal_unsaved": bool(self._journal_unsaved),
            }
            position = engine.position
            if position is None:
                status["position"] = None
            else:
                status["position"] = {
                    "spot_qty": (position.spot_held_qty
                                 if position.spot.base_fee_known else None),
                    "spot_filled_qty": position.spot.filled_qty,
                    "spot_qty_known": position.spot.base_fee_known,
                    "perp_short_qty": position.perp.filled_qty,
                    "net_delta_btc": (position.delta_qty
                                      if position.spot.base_fee_known else None),
                    "opened_ms": position.opened_ms,
                    "execution_mode": str(getattr(engine, "execution_mode", "")),
                }
            view = getattr(engine, "snapshot", None)
            if view is not None:
                status["market"] = {
                    "age_s": round(float(view.age_s), 1) if view.age_s is not None else None,
                    "margin_multiple": view.margin_multiple,
                    "basis_bps": view.basis_bps,
                }
            journal = getattr(self, "carry_journal", None)
            if journal is not None:
                # now_ms: without a clock an OPEN lifecycle's reconciliation
                # record never ages out, so a stale record would read as
                # reconciled here while carry_report (which passes a clock)
                # says it is not.
                st = journal.statement(now_ms=int(time.time() * 1000))
                status["accounting"] = {
                    k: st.get(k) for k in (
                        "net_basis", "net_usd", "fees_usd", "funding_usd",
                        "fees_confirmed", "funding_confirmed",
                        "funding_reconciled", "funding_unread",
                        "funding_unattributed", "funding_estimated_entries",
                        "open_perp_qty")}
                status["last_reconciliation_ms"] = self._last_reconcile_ms or None
            fault = ""
            if engine.state.name == "HALTED":
                fault = self._last_carry_decision.get("reason") or "HALTED"
            elif self._journal_unsaved:
                fault = "JOURNAL_WRITE_FAILED"
            status["blocking_fault"] = fault
            return status
        except Exception as exc:  # noqa: BLE001
            return {"attached": True, "blocking_fault": "STATUS_UNREADABLE",
                    "error": f"{type(exc).__name__}: {exc}"}

    def _memory_health(self) -> Dict[str, Any]:
        """A compact view of what the bot remembers, for the health endpoint.

        Read-only and credential-free. Health is served from a *different
        thread*, so this must never take the writer role — every call below is a
        read, which ``StateStore`` allows from any thread.

        A failure here degrades to a reason string rather than taking the whole
        health payload down: memory is diagnostic, and an operator checking
        whether the bot is naked should not be blocked by a broken aggregate.
        """
        if self.memory is None:
            return {"enabled": False}
        try:
            snapshot = self.memory.snapshot()
        except Exception as exc:  # noqa: BLE001
            return {"enabled": True, "error": f"{type(exc).__name__}: {exc}"}
        return {"enabled": True, **snapshot}

    def _policy_health(self) -> Dict[str, Any]:
        """What the learned policy is doing, if anything.

        Reported even when no model is loaded, because "off" and "loaded but
        refusing to propose" and "drifted" are three different situations and
        an operator needs to tell them apart at a glance. An absent key would
        make all three look the same.
        """
        snapshot = getattr(self.strategy, "snapshot", None)
        if not callable(snapshot):
            return {"mode": str(getattr(self.cfg, "POLICY_MODE", "off")),
                    "model_loaded": False}
        try:
            return snapshot()
        except Exception as exc:  # noqa: BLE001
            return {"error": f"{type(exc).__name__}: {exc}"}

    def start_health_server(self) -> Optional[int]:
        if not bool(getattr(self.cfg, "ENABLE_HEALTH_SERVER", False)):
            return None
        port = int(getattr(self.cfg, "HEALTHCHECK_PORT", 8081))
        host = str(getattr(self.cfg, "HEALTHCHECK_HOST", "127.0.0.1") or "127.0.0.1")
        handler = type("_Bound", (_HealthHandler,), {"bot": self})
        self._http = ThreadingHTTPServer((host, port), handler)
        self._http.daemon_threads = True
        threading.Thread(
            target=self._http.serve_forever, name="health", daemon=True
        ).start()
        logger.info("health server listening on %s:%d", host, port)
        return port

    # -- lifecycle ---------------------------------------------------------

    def startup(self) -> bool:
        """Prepare to trade. Returns False if trading must not begin."""
        self.started_epoch = time.time()

        # Claim the single-writer role for this thread. From here on, any other
        # thread that tries to mutate trading state raises rather than
        # interleaving. The health server is a reader and is unaffected.
        self.store.claim_writer()

        allowed, why = _config.assert_sandbox(self.cfg)
        if not allowed:
            logger.critical("startup blocked: %s", why)
            return False
        try:
            # The ACCOUNT is what the equity belongs to: the endpoint decides
            # that. PAPER_TRADING only changes whether orders are sent, not
            # whose balance is read, so flipping it must not wipe the peak.
            venue = getattr(self.cfg, 'BYBIT_VENUE', '')
            venue = venue.lower() if isinstance(venue, str) else ''
            scope = (f"venue={venue};"
                     f"testnet={bool(getattr(self.cfg, 'USE_TESTNET', True))}")
            if self.store.set_equity_scope(scope):
                logger.warning("equity scope changed to %s; yesterday's "
                               "equity will not seed today's anchor", scope)
        except Exception:  # noqa: BLE001
            logger.exception("could not record the equity scope")
        live_armed, live_why = _config.is_live_authorized(self.cfg)
        logger.warning(
            "mode: %s | testnet=%s paper=%s | %s",
            "LIVE" if live_armed else "SANDBOX",
            getattr(self.cfg, "USE_TESTNET", True),
            getattr(self.cfg, "PAPER_TRADING", True),
            live_why,
        )
        if live_armed:
            # The four-part env chain arms the PROCESS. It says nothing about
            # whether the STRATEGY has earned live. That second question has
            # its own authority (promotion_gate) and, until this check, nothing
            # on the order path asked it. Asked here, before any network call.
            allowed, why = self.live_promotion_check()
            if not allowed:
                logger.critical("startup blocked (live requested): %s", why)
                return False

        try:
            self.client.sync_time()
        except BybitAPIError as exc:
            logger.critical("cannot reach the exchange: %s", exc)
            return False

        # Leverage is set BEFORE reconciliation, because reconciliation may
        # need to close a position and the exchange rejects a reduce order whose
        # symbol has no leverage configured. On spot this is a no-op.
        # A process that was degraded to paper on the MAINNET endpoint (live
        # requested, not authorised) must not change the real account's
        # leverage either; neither may one that is about to refuse to start
        # on an engaged kill switch.
        mainnet_paper = (bool(getattr(self.cfg, "PAPER_TRADING", True))
                         and not bool(getattr(self.cfg, "USE_TESTNET", True)))
        if mainnet_paper:
            logger.warning("paper mode on the mainnet endpoint: leverage "
                           "is not changed")
        if (bool(getattr(self.cfg, "ALLOW_SHORTS", False))
                and not mainnet_paper
                and not self.store.is_kill_switch_engaged()[0]):
            for symbol in self.symbols:
                if not self.client.set_leverage(symbol):
                    logger.critical(
                        "could not set leverage for %s; refusing to start. A "
                        "position opened at the account's default leverage is a "
                        "position sized by something other than the risk model.",
                        symbol,
                    )
                    return False

        # THE CARRY BOOK'S COLD START (0037). Before anything else touches
        # the venue: what did the last run believe, and what is actually
        # there? A container that died holding a hedge used to come back FLAT
        # and open a second one against the same liquidation price.
        if self.carry is not None and not self.carry_cold_start():
            return False

        # Reconcile BEFORE any trading decision.
        try:
            summary = self.engine.reconcile()
            logger.warning("reconciliation: %s", summary)
            if summary.get("unknown"):
                logger.critical(
                    "%d order(s) could not be resolved against the exchange; "
                    "refusing to start", summary["unknown"]
                )
                return False
            if summary.get("orphan_positions"):
                logger.critical(
                    "venue holds position(s) the ledger does not know: %s; "
                    "refusing to start until a human reconciles them",
                    summary["orphan_positions"],
                )
                return False
        except Exception as exc:  # noqa: BLE001
            logger.critical("reconciliation failed: %s", exc, exc_info=True)
            return False
        self._reconciled = True

        engaged, reason = self.store.is_kill_switch_engaged()
        if engaged:
            logger.critical(
                "kill switch is engaged (%s). A human must clear it before "
                "trading resumes.", reason
            )
            return False

        try:
            days = int(getattr(self.cfg, "DECISIONS_RETENTION_DAYS", 90))
            pruned = self.store.prune_decisions(older_than_days=days)
            if pruned:
                logger.info("pruned %d decision rows older than %d days", pruned, days)
        except Exception as exc:  # noqa: BLE001
            logger.warning("decision journal prune skipped: %s", exc)

        self.start_health_server()
        # Private WS starts AFTER reconciliation has already established
        # ground truth (self._reconciled is True by this point) — a fast
        # observation path is only worth having once REST has confirmed
        # there is nothing left to observe cold.
        self._start_private_ws()
        # State what this process is before it does anything. Research is
        # closed and nothing has cleared Stage 1; the log must say so rather
        # than leave a reader to infer capability from silence.
        logger.warning("%s", _project_status.current(self.cfg).summary_line())
        self.session.start(reconciled=self._reconciled)
        return True

    def _start_private_ws(self) -> None:
        """Start the private-WS observer thread, only when explicitly
        enabled (`PRIVATE_WS_ENABLED`, default False — REST-only
        observation is the unchanged default behaviour). A failed init
        here must never look healthy: it degrades `observation_status`,
        never raises out of `startup()`, and REST remains fully able to
        run this process correctly with WS absent, exactly as it always
        has (see `tick()`'s unconditional observe_exits/
        check_naked_positions calls, which do not depend on this at all).
        """
        enabled = self._ws_enabled_override
        if enabled is None:
            enabled = bool(getattr(self.cfg, "PRIVATE_WS_ENABLED", False))
        if not enabled:
            self._observation_state = "DISABLED"
            return
        try:
            import private_ws_consumer as _pwc

            transport_factory = self._ws_transport_factory
            if transport_factory is None:
                import ws_transport as _wt
                url = self.client.ws_private_url
                transport_factory = lambda: _wt.PrivateWebSocket(url)  # noqa: E731

            evidence_path = self._ws_evidence_path or str(getattr(
                self.cfg, "WS_EVIDENCE_PATH", "") or _pwc.DEFAULT_WS_EVIDENCE_PATH)
            self.ws_consumer = _pwc.WSPrivateConsumer(
                client=self.client, transport_factory=transport_factory,
                evidence_path=evidence_path,
                assurance_mode=self._ws_assurance_mode)
            self.ws_stop_event = threading.Event()
            self.ws_thread = threading.Thread(
                target=self.ws_consumer.run_forever,
                args=(self.ws_stop_event,),
                name="private-ws", daemon=True,
            )
            self.ws_thread.start()
            self._observation_state = "STARTING"
        except Exception:  # noqa: BLE001
            logger.exception(
                "private-WS observer failed to start; continuing on "
                "REST-only observation")
            self.ws_consumer = None
            self.ws_thread = None
            self._observation_state = "DEGRADED"

    def _stop_private_ws(self) -> None:
        """Signal the consumer to stop and wait for its thread to
        ACTUALLY exit — not merely for as long as we're willing to wait.

        The receive loop can be blocked inside a single
        `transport.recv()` for up to `private_ws_consumer.
        RECV_TIMEOUT_SECONDS` before it next checks `ws_stop_event` (see
        that module's `run_once`/`run_forever`) — a join() timeout
        shorter than that is not a real synchronization boundary, it is a
        coin flip that happens to usually win. This joins for
        `RECV_TIMEOUT_SECONDS + WS_SHUTDOWN_SAFETY_MARGIN_SECONDS`, so a
        caller relying on this call meaning "the WS thread is genuinely
        stopped" (e.g. tools/testnet_conformance_run.py's
        `finalize_ws_lifecycle`, before it performs what MUST be the
        final drain) can trust that.

        If the thread is STILL alive after that — a real, unexpected
        shutdown failure, not the ordinary case — the thread reference is
        DELIBERATELY NOT discarded (a discarded-but-still-running thread
        could keep enqueueing observations nobody will ever drain) and
        `observation_status` is forced to DEGRADED so nothing downstream
        can read this as a clean shutdown.
        """
        if self.ws_thread is None:
            return
        self.ws_stop_event.set()
        if self.ws_consumer is not None:
            # the event alone is only read between connections; this makes a
            # live receive loop return within STOP_POLL_SECONDS
            self.ws_consumer.request_stop()
        import private_ws_consumer as _pwc

        shutdown_timeout = _pwc.RECV_TIMEOUT_SECONDS + WS_SHUTDOWN_SAFETY_MARGIN_SECONDS
        self.ws_thread.join(timeout=shutdown_timeout)
        if self.ws_thread.is_alive():
            logger.error(
                "private-WS thread did not stop within %.1fs (RECV_TIMEOUT_"
                "SECONDS=%.1fs + margin); this is a real shutdown failure, "
                "not a soft warning -- preserving the thread reference "
                "(never discarding it while still alive) and forcing "
                "observation_status to DEGRADED",
                shutdown_timeout, _pwc.RECV_TIMEOUT_SECONDS)
            self._observation_state = "DEGRADED"
            self.ws_shutdown_failed = True
            return
        self.ws_thread = None

    def _absorb_ws_observations(self, *, rest_ok: bool) -> None:
        """The WS -> REST-reconciliation bridge.

        `needs_reconciliation`/`dirty_symbols` (set by the WS consumer on
        disconnect, reconnect, or a position-topic event — see
        private_ws_consumer.py) are cleared ONLY after THIS cycle's REST
        calls (`observe_exits`/`check_naked_positions`, already run above,
        unconditionally, every tick) both completed without raising —
        never merely because a tick ran. `rest_ok` is computed by the
        caller from those two calls' own try/except blocks, not inferred
        here.
        """
        consumer = self.ws_consumer
        if consumer is None:
            return
        seen = getattr(self, "_ws_generation_at_tick", None)
        opened_later = (seen is not None and getattr(
            consumer, "reconcile_generation", seen) != seen)
        if consumer.needs_reconciliation:
            if rest_ok and not opened_later:
                consumer.needs_reconciliation = False
                consumer.dirty_symbols.clear()
                self._observation_state = "HEALTHY"
            else:
                self._observation_state = "DEGRADED"
        elif self.ws_thread is not None:
            self._observation_state = (
                "HEALTHY" if self.ws_thread.is_alive() else "DEGRADED")

    def live_promotion_check(self) -> "tuple[bool, str]":
        """May a LIVE-armed process proceed? ``(allowed, reason)``. Fails closed.

        Two conditions, both required, evaluated only when live is armed:

        1. ``promotion_gate.promotion_gate_allows_live()`` is True - every
           checklist item complete AND the live chain satisfied. The gate file
           is read from its canonical path; a missing or unreadable file
           refuses. The gate is never re-aimed from here.
        2. ``promotion_gate.strategy_is_the_cleared_signal`` accepts the
           attached strategy. A strategy without ``signal_name`` (the
           technical-analysis voter) has never been measured against a null
           and may not touch real money. No strategy at all is fine: the loop
           then reconciles and protects only. The research field itself is
           read by promotion_gate, not here, so it stays inert as a permission.

        This function cannot arm anything. It can only refuse.
        """
        try:
            import promotion_gate as _gate
            if not _gate.promotion_gate_allows_live(config=self.cfg):
                verdict = _gate.evaluate_promotion_gate(config=self.cfg)
                return False, "promotion gate refuses: " + "; ".join(
                    verdict.reasons[:3] or ["no reason recorded"])
        except Exception as exc:  # noqa: BLE001
            return False, f"promotion gate could not be evaluated ({exc}); refusing"

        try:
            import promotion_gate as _gate
            ok, why = _gate.strategy_is_the_cleared_signal(self.strategy,
                                                           config=self.cfg)
        except Exception as exc:  # noqa: BLE001
            return False, f"strategy identity could not be checked ({exc}); refusing"
        if not ok:
            return False, why
        return True, "promotion gate allows live and the cleared signal is attached"

    def run(self) -> int:
        """Blocking main loop. Returns a process exit code."""
        if not self.startup():
            return 1

        self._install_signal_handlers()
        logger.info("trading loop started for %s", ", ".join(self.symbols) or "(none)")

        self._last_tick_ok_epoch = time.time()     # the stall clock starts now
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001
                # One bad cycle must not kill the process, but it must be loud
                # and it must not be silently retried at full speed.
                logger.exception("cycle failed: %s", exc)
                self._tick_failures += 1
                self._stop.wait(min(30.0, self.loop_interval_s))
                continue
            self._tick_failures = 0
            self._last_tick_ok_epoch = time.time()
            self._stop.wait(self.loop_interval_s)

        self.shutdown()
        return 0

    def _persist_carry(self, state: Dict[str, Any]) -> None:
        """The engine's persist: journal first, then the position.

        The position is written the moment a leg lands; the journal used to be
        written only at the end of the tick. A kill in between left a position
        the next start resumed against a journal that had never seen the fill.
        A failure here raises into the engine, which halts on it.
        """
        journal = getattr(self, "carry_journal", None)
        # One commit: a kill between the two left the journal ahead of the
        # position row, which the next cold start can only halt on.
        transaction = getattr(self.store, "transaction", None)
        import contextlib as _contextlib
        with (transaction() if callable(transaction)
              else _contextlib.nullcontext()):
            if journal is not None:
                self.store.save_ledger(journal.to_rows())
            self.store.save_carry_position(state)

    def _log_venue_leverage(self, broker: Any) -> None:
        """Say what leverage the venue reports for the carry perp. Observation
        only: this process does not set, change or gate on it (the startup setter
        covers the directional symbols, not the carry perp), so without this line
        the number the book actually runs at is recorded nowhere."""
        reader = getattr(broker, "get_position_leverage", None)
        if reader is None:
            return
        try:
            leverage = reader(self.carry.perp_symbol)
        except Exception as exc:  # noqa: BLE001
            logger.warning("carry: could not read the venue's leverage for %s "
                           "(%s: %s)", self.carry.perp_symbol,
                           type(exc).__name__, exc)
            return
        logger.warning(
            "carry: the venue reports %s leverage on %s (observed only; this "
            "process neither set nor gates on it)",
            "UNSTATED" if leverage is None else f"{leverage:g}x",
            self.carry.perp_symbol)

    def carry_cold_start(self) -> bool:
        """Reconcile the carry ledger against the venue. False = do not start.

        Reads both sides and hands them to `CarryEngine.cold_start`, which is
        pure and has its own tests. Anything unreadable is a refusal: a
        restart that cannot see the venue must not assume it is flat.
        """
        broker = self.carry.broker
        try:
            ledger = self.store.load_carry_position()
        except Exception as exc:  # noqa: BLE001
            logger.critical(
                "carry cold start: the ledger is unreadable (%s); refusing to "
                "start", exc)
            return False
        try:
            mode_of = getattr(broker, "get_position_mode", None)
            if mode_of is not None and \
                    mode_of(self.carry.perp_symbol) != "one_way":
                logger.critical(
                    "carry cold start: %s is in hedge mode; this adapter sends "
                    "no positionIdx, so every order (including the buy-back of "
                    "a held short) would be rejected. Switch the account to "
                    "one-way mode; refusing to start", self.carry.perp_symbol)
                return False
            self._log_venue_leverage(broker)
            read_started_ms = int(time.time() * 1000)
            venue_perp = float(broker.get_perp_position(self.carry.perp_symbol))
            venue_spot = float(
                broker.get_spot_inventory(self.carry.spot_symbol))
            open_orders = broker.get_open_carry_orders(
                self.carry.spot_symbol, self.carry.perp_symbol)
            # ONE read decides RESUME / CLEAN / HALT, and a position list that
            # lags (a crash right after a fill, a replica behind the matching
            # engine) can say flat while the venue is short: CLEAN, then a
            # second hedge. Read both sizes again after a pause; reads that
            # disagree are not a state to start from.
            if self.CARRY_COLD_START_REREAD_S > 0:
                time.sleep(self.CARRY_COLD_START_REREAD_S)
            again_perp = float(broker.get_perp_position(self.carry.perp_symbol))
            again_spot = float(
                broker.get_spot_inventory(self.carry.spot_symbol))
            # A position does not accrue; a wallet balance can (interest, a
            # client deposit), so the spot comparison is relative.
            spot_tol = max(1e-9, 1e-7 * max(abs(venue_spot), abs(again_spot)))
            if (abs(again_perp - venue_perp) > 1e-9
                    or abs(again_spot - venue_spot) > spot_tol):
                logger.critical(
                    "carry cold start: two reads of the venue disagree (perp "
                    "%.10g then %.10g, spot %.10g then %.10g); refusing to "
                    "start from a state that is still moving", venue_perp,
                    again_perp, venue_spot, again_spot)
                return False
        except Exception as exc:  # noqa: BLE001
            logger.critical(
                "carry cold start: the venue is unreadable (%s); refusing to "
                "start rather than assuming the book is flat", exc)
            return False

        # WARM THE EWMA (0045). The venue has been publishing settled prints
        # the whole time this process was not running; without them a fresh
        # deploy stands aside for 24 hours with INSUFFICIENT_FUNDING_HISTORY.
        # A venue that will not hand them over is not a reason to refuse to
        # start — it just means the wait — so this logs and carries on.
        try:
            venue_prints = broker.get_funding_history(
                self.carry.perp_symbol, limit=30)
            self.carry.venue_prints_at_start = list(venue_prints)
            warmed = self.carry.warm_funding_history(venue_prints)
            if warmed:
                logger.warning("carry: EWMA warmed with %d settled prints",
                               warmed)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "carry: could not warm the funding history (%s); the book "
                "will stand aside until it has seen %s prints itself", exc,
                self.carry.FUNDING_HISTORY)

        decision = self.carry.cold_start(
            ledger=ledger, venue_perp_qty=venue_perp, venue_spot_qty=venue_spot,
            open_orders=open_orders, venue_read_started_ms=read_started_ms)
        logger.warning(
            "carry cold start: %s — %s (ledger=%s venue_perp=%.10g "
            "venue_spot=%.10g open_orders=%d)",
            decision.action.value, decision.reason,
            (ledger or {}).get("book_state", "NONE"), venue_perp, venue_spot,
            len(open_orders))
        if decision.action.value == "HALT":
            logger.critical(
                "carry cold start HALTED — a human must reconcile before this "
                "process trades: %s", decision.reason)
            return False
        journal_rows = None
        if decision.action.value == "RESUME" and \
                self.carry.position is not None:
            try:
                journal_rows = self.store.load_ledger()
            except Exception as exc:  # noqa: BLE001
                logger.critical("carry cold start: the journal is unreadable "
                                "(%s: %s)", type(exc).__name__, exc)
                journal_rows = []
        if (decision.action.value == "RESUME" and self.carry.position is not None
                and not journal_rows):
            # `_persist_carry` writes the journal BEFORE the position, so a
            # held pair always has fills in the journal. An empty one is a lost
            # or truncated ledger: no lifecycle exists to book the venue's
            # funding against, no fill to realise a close from, and nothing
            # would say so. Refuse rather than run an unaccountable book.
            logger.critical(
                "carry cold start: the ledger holds a resumed pair but the "
                "double-entry journal is empty; its fills, fees and funding "
                "cannot be accounted. Refusing to start - a human must "
                "reconcile the journal")
            self.carry._halt(
                "JOURNAL_EMPTY_FOR_HELD_PAIR",
                "a resumed pair has no journal entries")
            return False
        return True

    def tick(self) -> None:
        """One cycle: refresh equity, check the brakes, then consider each symbol."""
        self.session.tick()
        # A HELD carry pair must be watched whatever the account-level brakes
        # say. The equity read and the risk layer's halt are directional-book
        # controls; neither can excuse skipping the margin and liquidation
        # checks of a short that is live at the venue. They still stop NEW
        # exposure: with no pair held, both returns below are unchanged.
        carry_pair_held = (self.carry is not None
                           and getattr(self.carry, "position", None) is not None)
        try:
            equity = self.client.get_equity()
            self.risk.update_equity(equity)
        except Exception as exc:  # noqa: BLE001
            # Not only BybitAPIError: the equity step also writes the store
            # (PersistenceError: disk full, locked file) and parses the wallet
            # (ValueError). None of those may skip the held pair's checks.
            logger.error("equity unreadable this cycle (%s: %s)",
                         type(exc).__name__, exc)
            if not carry_pair_held:
                return

        # Apply any WS-observed order/execution events FIRST, on this
        # (the writer) thread — persistence.StateStore enforces a single
        # writer thread, so the WS consumer's own background thread never
        # calls a store-mutating method itself; it only queues (see
        # private_ws_consumer.py's module docstring).
        if self.ws_consumer is not None:
            self.ws_consumer.drain_and_apply(self.store)

        # Look at what we HOLD before deciding what to do. A stop or take-profit
        # that filled at the venue since the last cycle must reach the ledger
        # before any gate reads open_position_count or the loss streak.
        rest_observation_ok = True
        try:
            late = self.engine.resolve_unresolved_entries()
            if late.get("resolved") or late.get("adopted"):
                logger.warning("unresolved entries settled this cycle: %s", late)
        except Exception as exc:  # noqa: BLE001
            logger.exception("resolve_unresolved_entries failed; continuing: %s", exc)
        consumer = getattr(self, "ws_consumer", None)
        self._ws_generation_at_tick = getattr(
            consumer, "reconcile_generation", None)
        try:
            observed = self.engine.observe_exits()
            if observed.get("closed") or observed.get("reduced"):
                logger.warning("exits observed this cycle: %s", observed)
            if observed.get("unread") or observed.get("stop_dead"):
                # A position the venue could not be asked about was not
                # observed (the gap the WS flagged is still open), and a dead
                # stop is a state the next cycle must look at again. (A
                # phantom-coin row, "stop_dead_unverified", is alarmed once and
                # left for reconcile; counting it here would hold the whole
                # observation state at DEGRADED for every symbol until then.)
                rest_observation_ok = False
        except Exception as exc:  # noqa: BLE001
            logger.exception("observe_exits failed; continuing: %s", exc)
            rest_observation_ok = False

        # Exits booked without an authoritative price are priced from the
        # venue's own fills as soon as it will state them (read-only; a no-op
        # when nothing is provisional).
        try:
            if self.store.provisional_trades(limit=1):
                recon = self.engine.reconcile_provisional_exits()
                if recon.get("reconciled"):
                    logger.warning("provisional exits reconciled: %s", recon)
        except Exception as exc:  # noqa: BLE001
            logger.exception("reconcile_provisional_exits failed; "
                             "continuing: %s", exc)

        # Exits booked without their execution ids (a fill list that had not
        # caught up, an order filled in pieces across cycles) are proven later;
        # read-only, spaced by doubling back-off, a no-op when nothing is owed.
        try:
            proof = self.engine.retry_missed_exit_proofs()
            if proof.get("proven") or proof.get("given_up"):
                logger.warning("exit proofs: %s", proof)
        except Exception as exc:  # noqa: BLE001
            logger.exception("retry_missed_exit_proofs failed; continuing: %s", exc)

        # An exit that stays unpriced past the review window is a result nobody
        # can see: alarm once per row (journal) and keep shouting in the log;
        # the risk gate refuses new entries meanwhile.
        try:
            self._alarm_unpriced_exits()
        except Exception as exc:  # noqa: BLE001
            logger.exception("unpriced-exit alarm failed; continuing: %s", exc)

        # Funding the venue settled against closed directional linear trades
        # is booked from the account's own settlement records (read-only,
        # throttled; unreadable evidence leaves the trade unbooked and says so).
        try:
            self._book_directional_funding()
        except Exception as exc:  # noqa: BLE001
            logger.exception("directional funding reconciliation failed; "
                             "continuing: %s", exc)

        # A stop that existed and was later cleared AT THE VENUE while the
        # position stayed open is invisible to reconcile()'s ledger-only
        # check, which only runs once at startup. This is the live-venue
        # counterpart, checked every cycle, before any new entry is decided.
        try:
            naked = self.engine.check_naked_positions()
            if naked.get("reprotected") or naked.get("flattened"):
                logger.warning("naked positions handled this cycle: %s", naked)
            if (naked.get("unread") or naked.get("skipped")
                    or naked.get("flatten_failed")):
                rest_observation_ok = False
        except Exception as exc:  # noqa: BLE001
            logger.exception("check_naked_positions failed; continuing: %s", exc)
            rest_observation_ok = False

        # The WS -> REST bridge: a gap the WS consumer flagged (disconnect,
        # reconnect, a position-topic event) is cleared only now that this
        # cycle's two REST calls above have both been attempted, and only
        # if neither raised — never merely because a tick ran.
        self._absorb_ws_observations(rest_ok=rest_observation_ok)

        if self.risk.should_halt_trading():
            if not carry_pair_held:
                logger.warning("trading halted by the risk layer this cycle")
                return
            logger.warning(
                "risk layer halt in force; the held carry pair is still "
                "monitored (no new exposure can open while it is held)")

        # ---- CARRY BOOK ----------------------------------------------------
        # When the carry book is attached it owns the cycle and returns. It is
        # not one voter among several: holding a delta-neutral pair AND a
        # directional position against the same margin is two strategies
        # fighting over one liquidation price.
        #
        # Mark and funding are read from the VENUE, not the corpus. The corpus
        # is history; this decision is about the next eight hours. Both reads
        # raise when unreadable and the raise reaches CarryEngine as a halt,
        # because a book that trades on absent data is the failure this whole
        # system exists to prevent.
        if self.carry is not None:
            # The symbol the book TRADES, never TRADING_SYMBOLS[0]: config
            # accepts the two independently, and reading one symbol's mark,
            # funding print and margin to size orders on another is trading on
            # the wrong market's data.
            symbol = self.carry.perp_symbol
            # ONE observation, not three loose reads. The basis is a DIFFERENCE
            # between the perp and spot marks, so reading them seconds apart
            # measures the basis plus the drift between the calls. take_snapshot
            # stamps all four reads with a single observation time and
            # assert_fresh refuses a view that is stale or whose clock disagrees
            # with the venue — a frozen feed is indistinguishable from a quiet
            # market without that check.
            try:
                from market_snapshot import take_snapshot
                view = take_snapshot(self.carry.broker, perp_symbol=symbol,
                                     spot_symbol=self.carry.spot_symbol)
                view.assert_fresh()
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "carry market view unusable this cycle (%s); the book is "
                    "NOT touched and no order is sent", exc)
                self._last_carry_decision = {
                    "action": "skipped", "reason": "MARKET_VIEW_UNUSABLE",
                    "error": f"{type(exc).__name__}: {exc}",
                    "at_ms": int(time.time() * 1000)}
                # A held pair is not blinded by a bad view: the venue's own
                # size is read directly and a drifted or unreadable short
                # halts the book now, not when the feed happens to recover.
                try:
                    watched = self.carry.check_venue_position_without_a_view()
                except Exception as watch_exc:  # noqa: BLE001
                    # The viewless watch can reach an unwind (orders, persist). An
                    # exception here used to fall to run()'s generic handler,
                    # outside the held-pair error counter: a held pair with a
                    # broken feed and a failing unwind was retried forever.
                    logger.exception("carry viewless watch raised: %s", watch_exc)
                    if getattr(self.carry.state, "name", "") in (
                            "OPENING", "UNWINDING"):
                        self.carry._halt(
                            "ENGINE_EXCEPTION_MID_TRANSACTION",
                            f"{type(watch_exc).__name__}: {watch_exc}")
                    elif self.carry.position is not None:
                        self._carry_held_errors += 1
                        if (self._carry_held_errors
                                >= CARRY_MAX_CONSECUTIVE_HELD_ERRORS):
                            self.carry._halt(
                                "HELD_BOOK_UNMANAGEABLE",
                                f"the viewless watch raised on "
                                f"{self._carry_held_errors} consecutive ticks "
                                f"while a pair was held "
                                f"({type(watch_exc).__name__}: {watch_exc})")
                    return
                if watched is not None:
                    self._last_carry_decision = {
                        "action": str(watched.action),
                        "reason": str(watched.reason),
                        "state": watched.state.value,
                        "at_ms": int(time.time() * 1000)}
                    logger.critical(
                        "carry book HALTED without a market view: %s",
                        watched.reason)
                return
            mark, spot = view.perp_mark, view.spot_mark
            # An accounting journal that cannot be written down is a book whose
            # fills, fees and funding will not survive a restart. While a pair
            # is held the book keeps watching it (monitoring outranks
            # bookkeeping); with nothing held it must not take on new
            # exposure it cannot account for.
            if (self._journal_unsaved and self.carry.position is None
                    and getattr(getattr(self, "carry_journal", None), "to_rows", None)
                    and getattr(self.carry.state, "name", "") != "HALTED"):
                # One transient store error is not a reason to stand down for
                # good: write it again first, and halt only if that fails too.
                try:
                    self.store.save_ledger(self.carry_journal.to_rows())
                    self._journal_unsaved = False
                except Exception:  # noqa: BLE001
                    logger.exception("the ledger retry also failed")
            if (self._journal_unsaved and self.carry.position is None
                    and getattr(self.carry.state, "name", "") != "HALTED"):
                self.carry._halt(
                    "JOURNAL_WRITE_FAILED",
                    "the double-entry journal could not be written on the "
                    "previous cycle and the book is flat; refusing new "
                    "exposure it cannot account for")
                return
            perp_held = getattr(view, "perp_held", None)
            if (perp_held is not None and self.carry.position is None
                    and abs(float(perp_held)) > 1e-9
                    and getattr(self.carry.state, "name", "") == "FLAT"):
                # A short the book never recorded halts the book (and trips the
                # kill switch) - so it must be a short that is really there. The
                # position list can still show the size of a hedge the engine
                # closed a moment ago (an overlay inventory exit never re-reads
                # the venue): look again after a pause and halt only on two
                # reads that agree. An unreadable second read skips the check this tick.
                time.sleep(self.CARRY_ORPHAN_REREAD_S)
                try:
                    perp_held = float(self.carry.broker.get_perp_position(
                        self.carry.perp_symbol))
                except Exception as exc:  # noqa: BLE001
                    # One stale reading cannot halt a book on its own and the
                    # confirming read failed: unconfirmed is not an orphan. Skip
                    # the check this tick; the next tick reads again, so a real
                    # orphan is delayed by one tick, never missed.
                    # The tick must not TRADE on that either: skipping the check
                    # while still opening would put a second hedge on top of a
                    # short the book never recorded. Unconfirmed means stand
                    # still this tick and read again on the next.
                    logger.warning("carry: orphan re-read failed (%s); standing "
                                   "still this tick", exc)
                    self._last_carry_decision = {
                        "action": "skipped", "reason": "ORPHAN_UNCONFIRMED",
                        "state": getattr(self.carry.state, "value", "FLAT"),
                        "at_ms": int(time.time() * 1000)}
                    return
            orphan = self.carry.check_venue_flat_while_flat(perp_held)
            if orphan is not None:
                self._last_carry_decision = {
                    "action": str(orphan.action), "reason": str(orphan.reason),
                    "state": orphan.state.value,
                    "at_ms": int(time.time() * 1000)}
                logger.critical("carry book HALTED while FLAT: %s",
                                orphan.reason)
                return
            self.carry.snapshot = view
            # The SETTLED print and its stamp, not the ticker's forecast
            # (0034). The loop ticks every LOOP_INTERVAL_SECONDS; a print
            # happens every eight hours. The stamp is how the engine tells
            # them apart.
            try:
                decision = self.carry.on_candle(
                    mark=mark, funding_bps=view.funding_print_bps, spot=spot,
                    timestamp_ms=int(time.time() * 1000),
                    funding_print_ms=view.funding_print_ms)
            except Exception as exc:  # noqa: BLE001
                # An exception that escapes the engine in the middle of a
                # two-leg transaction leaves a leg the engine no longer tracks
                # and a state of OPENING/UNWINDING with no position. The next
                # tick must not treat that as flat and open again: stop, write
                # it down, and let cold start compare the ledger with the venue.
                logger.exception("carry engine raised: %s", exc)
                if getattr(self.carry.state, "name", "") in (
                        "OPENING", "UNWINDING"):
                    self.carry._halt(
                        "ENGINE_EXCEPTION_MID_TRANSACTION",
                        f"{type(exc).__name__}: {exc}")
                    logger.critical(
                        "carry book HALTED mid-transaction - a human must "
                        "reconcile the venue against the ledger")
                elif self.carry.position is not None:
                    self._carry_held_errors += 1
                    if (self._carry_held_errors
                            >= CARRY_MAX_CONSECUTIVE_HELD_ERRORS):
                        self.carry._halt(
                            "HELD_BOOK_UNMANAGEABLE",
                            f"the engine raised on {self._carry_held_errors} "
                            f"consecutive ticks while a pair was held "
                            f"({type(exc).__name__}: {exc})")
                        logger.critical(
                            "carry book HALTED: a held pair could not be "
                            "managed for %d ticks - a human must reconcile",
                            self._carry_held_errors)
                return
            self._carry_held_errors = 0
            self._last_carry_decision = {
                "action": str(decision.action), "reason": str(decision.reason),
                "state": decision.state.value,
                "at_ms": int(time.time() * 1000)}
            logger.info(
                "carry: %s (%s) state=%s perp=%.2f spot=%.2f "
                "basis=%.1fbps settled=%.3fbps@%s predicted=%.3fbps %s",
                decision.action, decision.reason, decision.state.value,
                mark, spot, (mark / spot - 1.0) * 1e4,
                view.funding_print_bps, view.funding_print_ms,
                view.funding_bps, decision.detail or "")
            # THE LEDGER (0044). Fills book themselves through LedgerBroker.
            # FUNDING is booked from what the VENUE paid (the account's
            # settlement rows), never from the engine's own rate x qty x mark:
            # that number is an estimate and the engine says so (its mark is
            # not the settlement mark). The engine's figure is kept only to
            # compare against.
            journal = getattr(self, "carry_journal", None)
            if journal is not None:
                self._book_carry_funding(journal, symbol, decision)
                self._accrue_carry_borrow(journal, spot)
                try:
                    self.store.save_ledger(journal.to_rows())
                    self._journal_unsaved = False
                except Exception:  # noqa: BLE001
                    self._journal_unsaved = True
                    logger.exception("the ledger could not be written down")

            if decision.state.name == "HALTED":
                logger.critical(
                    "carry book HALTED — a human must clear it: %s",
                    decision.reason)
            return

        # The time exit, BEFORE entries. A position at its horizon must leave
        # this cycle even if the same cycle would open another.
        if self.shadow_horizon_days:
            self._expire_at_horizon()

        if self.strategy is None:
            # No signal source. Take no trades and say so, rather than inventing
            # one — the legacy fallback fabricated BUYs at confidence 0.66.
            logger.debug("no strategy attached; no signals evaluated")
            return

        for symbol in self.symbols:
            if self._stop.is_set():
                return
            if not self.risk.can_trade(symbol):
                continue
            try:
                sync = getattr(self.strategy, "sync_from_store", None)
                if callable(sync):
                    sync(self.store)
                intent = self.strategy.signal_for(symbol)
            except Exception as exc:  # noqa: BLE001
                logger.exception("strategy failed for %s: %s", symbol, exc)
                continue
            if intent is None:
                continue
            # The signal is computed from the last CLOSED bar, so it is the
            # same for every tick until the next bar closes. Once it has been
            # traded, an exit (a stop hit minutes later) must not be followed
            # by the identical entry on the very next tick: that repeats a
            # round trip, fees included, up to once a tick for the whole bar.
            rows = getattr(self.strategy, "last_rows", None)
            bar = None
            try:
                bar = rows[-1][0] if rows else None
            except (TypeError, IndexError, KeyError):
                bar = None
            signature = (bar, intent.signal_type, float(intent.entry_price),
                         float(intent.stop_price), tuple(intent.take_profits))
            traded = self.__dict__.setdefault("_traded_signals", {})
            signature = json.dumps(signature, default=str)
            if bar is None:
                # No bar to key on: dedupe within this process only; a stored
                # bar-less signature would suppress later signals for ever.
                traded.setdefault(symbol, "")
            elif symbol not in traded:
                # survives a restart: the same bar's signal is not re-entered
                # just because the process was bounced after a stop-out
                try:
                    traded[symbol] = self.store.traded_signal(symbol)
                except Exception:  # noqa: BLE001
                    traded[symbol] = ""
            if traded.get(symbol) == signature:
                logger.debug("%s: signal already traded on this bar", symbol)
                continue
            report = self.engine.execute(intent)
            if report.ok or report.stage in ("emergency", "fill"):
                # Anything that reached the venue counts as traded: an entry
                # closed again by the safety path is not to be retried on the
                # same bar either.
                traded[symbol] = signature
                if bar is not None:
                    try:
                        self.store.mark_traded_signal(symbol, signature)
                    except Exception:  # noqa: BLE001
                        logger.exception("could not persist the traded signal")
            logger.info("%s -> %s (%s)", symbol, report.reason, report.stage)

    #: Class-level defaults so a bot built without __init__ (tests, tools) works.
    _carry_estimated_funding = 0.0
    _last_funding_poll_ms = 0
    _journal_unsaved = False
    #: Pause between the two cold-start reads of the venue (see carry_cold_start).
    CARRY_COLD_START_REREAD_S = 0.25
    #: Pause before the orphan-position read is repeated (see tick).
    CARRY_ORPHAN_REREAD_S = 0.5
    _carry_held_errors = 0
    _last_carry_decision: Dict[str, Any] = {}

    #: How often to read the venue's settlement rows. Funding settles every 8h,
    #: so a few minutes is more than enough and keeps the call count low.
    FUNDING_POLL_MS = 300_000

    #: A full read of every pending lifecycle's interval (both venue reads) is
    #: heavier than the incremental poll, so it runs at most this often - and
    #: at once whenever a lifecycle has no current reconciliation record.
    RECONCILE_POLL_MS = 3_600_000
    _last_reconcile_ms = 0

    #: Borrow accrues continuously; it is posted in chunks of at least this
    #: long (and in full when the pair closes), so the journal does not take
    #: an entry per tick.
    BORROW_POST_EVERY_MS = 3_600_000
    _borrow_since_ms: Optional[int] = None
    _borrow_notional_usd = 0.0

    def _accrue_carry_borrow(self, journal: Any, spot_mark: float) -> None:
        """Book the financing cost of an ACQUIRE pair into the journal.

        The engine's entry gate prices borrow (`borrow_apr`) but the live
        journal never booked it, so `net_usd` ignored a real cost of the
        financed spot leg. Overlay holds the client's own coin: no borrow.
        Never raises into the loop.
        """
        try:
            carry = self.carry
            if carry is None or carry.execution_mode != "acquire":
                return
            apr = float(carry.borrow_apr)
            if not (apr > 0.0):
                return
            now = int(time.time() * 1000)
            position = carry.position
            if position is not None:
                spot_leg = position.spot
                notional = (float(spot_leg.filled_qty)
                            * float(spot_mark or spot_leg.avg_price or 0.0))
                opened = int(position.opened_ms or now)
                if (self._borrow_since_ms is not None
                        and opened > self._borrow_since_ms):
                    self._borrow_since_ms = None        # a different pair
                if self._borrow_since_ms is None:
                    # Resume after the last borrow the journal already holds:
                    # a restart must not book the same hours twice.
                    posted = max((e.ms for e in journal.entries
                                  if getattr(e, "kind", "") == "borrow"),
                                 default=0)
                    self._borrow_since_ms = max(opened, int(posted))
                    self._borrow_notional_usd = notional
                    return
                if now - self._borrow_since_ms < self.BORROW_POST_EVERY_MS:
                    self._borrow_notional_usd = notional
                    return
            elif self._borrow_since_ms is None:
                return
            import ledger as _ledger
            held_ms = max(0, now - self._borrow_since_ms)
            usd = (self._borrow_notional_usd * apr
                   * held_ms / (365.0 * 86_400_000.0))
            if usd > 0.0:
                newest = max((e.ms for e in journal.entries), default=0)
                _ledger.borrow(journal, ms=max(now, int(newest)),
                               ref=f"borrow:{now}", usd=usd)
            if position is not None:
                self._borrow_since_ms = now
                self._borrow_notional_usd = notional
            else:
                self._borrow_since_ms = None
                self._borrow_notional_usd = 0.0
        except Exception:  # noqa: BLE001
            logger.exception("borrow accrual failed; the cost is not booked")

    UNPRICED_ALARM_EVERY_S = 600.0
    _last_unpriced_alarm = 0.0

    def _alarm_unpriced_exits(self) -> None:
        status = self.store.unpriced_exit_status()
        if not status["stuck"]:
            return
        for tid in status["stuck"]:
            self.store.mark_unpriced_alarm(tid)
        now = time.time()
        if now - self._last_unpriced_alarm >= self.UNPRICED_ALARM_EVERY_S:
            self._last_unpriced_alarm = now
            logger.critical(
                "UNPRICED EXIT STUCK: trade(s) %s have had no authoritative "
                "price for over %.0f h (oldest %.1f h). Their results are "
                "invisible to sizing and the loss brakes; new entries are "
                "REFUSED until the venue prices them or a human acknowledges "
                "them (StateStore.acknowledge_unpriced_exit).",
                status["stuck"], status["review_after_s"] / 3600.0,
                status["oldest_age_s"] / 3600.0)

    DIRECTIONAL_FUNDING_POLL_MS = 10 * 60 * 1000
    _last_directional_funding_ms = 0

    def _book_directional_funding(self) -> None:
        if not getattr(self.engine.client, "is_linear", False):
            return
        now_ms = int(time.time() * 1000)
        if now_ms - self._last_directional_funding_ms < self.DIRECTIONAL_FUNDING_POLL_MS:
            return
        if not self.store.trades_awaiting_funding(limit=1):
            return
        self._last_directional_funding_ms = now_ms
        import directional_funding as _df
        from carry_broker import CarryBroker
        reader = CarryBroker(client=self.engine.client,
                             sequence_source=lambda *a: 0,
                             order_gate=lambda *a, **k: (False, "read-only"))
        shared = ({self.carry.perp_symbol} if self.carry is not None else set())
        res = _df.reconcile_directional_funding(
            self.store, reader, now_ms=now_ms, carry_symbols=shared)
        if res["booked"] or res["no_settlement"] or res["left"]:
            logger.warning("directional funding: %s", res)

    def _book_carry_funding(self, journal: Any, symbol: str,
                            decision: Any) -> None:
        """Book funding the venue actually paid, once each, attributed to the book.

        Fails visible: an unreadable venue while a position is open writes an
        `unread` marker (persisted, so the statement cannot claim confirmed
        funding), and a payment the book cannot claim is journaled at $0 as
        `unattributed` with its evidence. Completeness is established per
        position lifecycle (`funding_reconcile`) and persisted in the journal.
        Never raises into the loop.
        """
        import ledger as _ledger
        import funding_reconcile as _recon

        collected = float((decision.detail or {}).get("collected", 0.0))
        self._carry_estimated_funding = (
            0.0 if decision.action == "unwound" else collected)

        # The judgement clock never precedes the journal's own newest event. A
        # read window that ends before the lifecycle it covers began (the wall
        # clock a millisecond behind the stamp of the fill that opened it) is
        # not "unreadable"; it is an inverted interval, and calling it that
        # recorded a spurious FUNDING_HISTORY_UNREADABLE for up to an hour.
        newest = max((e.ms for e in journal.entries), default=0)
        now_ms = max(int(time.time() * 1000), int(newest))
        pending = _recon.pending_lifecycles(journal)
        if not pending:
            return
        status = {r["lifecycle_id"]: r for r in
                  _ledger.lifecycle_reconciliation(journal, now_ms=now_ms)}
        needs_record = any(
            status[lc.id]["state"] in (
                "NOT_RECONCILED", "STALE", "PENDING_DELIVERY_GRACE",
                "CONTRADICTED_AFTER_RECONCILIATION", "INCONSISTENT_RECORD",
                "STALE_AS_OF")
            for lc in pending)
        # One throttle for every venue read here, including the first read
        # after a fill: a record that cannot be written must not become a
        # request storm.
        if now_ms - self._last_funding_poll_ms < self.FUNDING_POLL_MS:
            return
        full_due = (needs_record or
                    now_ms - self._last_reconcile_ms >= self.RECONCILE_POLL_MS)
        self._last_funding_poll_ms = now_ms

        earliest = min(lc.start_ms for lc in pending)
        evidence = history = None
        if full_due:
            self._last_reconcile_ms = now_ms
            since = max(0, earliest - _ledger.ATTRIBUTION_SLACK_MS)
        else:
            seen_ms = [int(e.fee_raw.get("venue_ms", 0)) for e in journal.entries
                       if e.basis in ("venue", "unattributed")]
            # A day of overlap, not an hour: a settlement row delivered late
            # (the docs warn of delays) must still fall inside the next poll's
            # window. Re-reading booked rows is free: they dedupe by identity.
            since = max(0, (max(seen_ms) - 86_400_000) if seen_ms
                        else earliest - _ledger.ATTRIBUTION_SLACK_MS)
        broker = self.carry.broker
        try:
            evidence = broker.get_funding_evidence(symbol, since, now_ms)
        except Exception as exc:  # noqa: BLE001
            logger.error("funding could not be read from the venue (%s); not "
                         "booked, and the statement is marked unverified", exc)
            try:
                _ledger.mark_funding_unread(journal, ms=now_ms, why=str(exc))
            except Exception:  # noqa: BLE001
                logger.exception("funding-unread marker could not be written")
        if evidence is not None:
            if not evidence.complete:
                logger.error("funding: the settlement log read is incomplete "
                             "(%s); valid rows are booked, completeness is "
                             "not claimed", "; ".join(evidence.errors[:2]))
                try:
                    _ledger.mark_funding_unread(
                        journal, ms=now_ms,
                        why="; ".join(evidence.errors[:2]))
                except Exception:  # noqa: BLE001
                    logger.exception("funding-unread marker not written")
            try:
                booked = _ledger.book_funding_payments(
                    journal, evidence.payments, ms=now_ms)
            except _ledger.LedgerError:
                logger.exception("funding payments could not be booked")
                return
            if booked["id_collisions"]:
                logger.warning("funding: %d payment(s) reused a venue id with a "
                               "different payload; kept as evidence, NOT "
                               "booked as income", booked["id_collisions"])
            if booked["booked"] or booked["unattributed"]:
                logger.warning(
                    "carry funding: booked %d venue payment(s), %d "
                    "unattributed; venue-observed total $%.4f (engine's "
                    "estimate for the open hedge: $%.4f, NOT booked)",
                    booked["booked"], booked["unattributed"],
                    journal.statement()["funding_venue_usd"],
                    self._carry_estimated_funding)
        if full_due:
            try:
                history = broker.get_funding_history_range(
                    symbol, earliest, now_ms)
            except Exception as exc:  # noqa: BLE001
                logger.warning("funding history not read this poll (%s)", exc)
            try:
                for rec in _recon.reconcile_funding(
                        journal, history=history,
                        evidence=evidence, now_ms=now_ms):
                    logger.warning("funding reconciliation %s: %s",
                                   rec["state"], ", ".join(rec["reasons"])
                                   or "complete")
            except Exception:  # noqa: BLE001
                logger.exception("funding reconciliation could not be recorded")

    def _expire_at_horizon(self) -> None:
        """Close any shadow position that has reached HORIZON daily bars.

        The rule holds for at most `HORIZON` bars and then leaves at the close,
        whatever the price. Nothing in the runtime enforced that: the engine
        has no time exit at all, only maker timeouts. A position left to run to
        its stop or its target is a DIFFERENT OBJECT from the one the cleared
        measurement scored, and a monitor watching the wrong object is worse
        than no monitor because it looks like diligence.

        Counted in whole UTC days rather than in bars, which is the same thing
        for a daily rule and needs no bar plumbing: the position's
        `opened_epoch` is wall-clock, and a daily bar is a UTC day.

        Best effort by design. A close that fails is logged and retried next
        cycle rather than raising into the loop — the position keeps its
        protective stop either way, so a failure here costs time, not safety.
        """
        horizon = int(self.shadow_horizon_days or 0)
        if horizon <= 0:
            return
        today = dt.datetime.now(dt.timezone.utc).date()
        try:
            rows = list(self.store.open_positions())
        except Exception:  # noqa: BLE001
            logger.exception("could not read positions for the horizon exit")
            return
        for row in rows:
            try:
                opened = float(row.get("opened_epoch") or 0.0)
                if opened <= 0:
                    continue
                entry_day = dt.datetime.fromtimestamp(
                    opened, dt.timezone.utc).date()
                held = (today - entry_day).days
                if held < horizon:
                    continue
                symbol = str(row["symbol"])
                report = self.engine.close_position(
                    symbol=symbol, reason=f"HORIZON_{horizon}_BARS")
                logger.warning(
                    "shadow horizon exit: %s held %d days (horizon %d) -> %s",
                    symbol, held, horizon, report.reason)
            except Exception:  # noqa: BLE001
                logger.exception("horizon exit failed for %r", row.get("symbol"))

    # -- shutdown ----------------------------------------------------------

    def _install_signal_handlers(self) -> None:
        def handle(signum: int, _frame: Any) -> None:
            logger.warning("signal %s received; shutting down", signum)
            self._stop.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, handle)
            except ValueError:
                # Not the main thread (e.g. under a test runner). Fine.
                pass

    def shutdown(self) -> None:
        """Graceful stop. Protective stops are deliberately left in place."""
        logger.warning("shutting down")
        # WS stops FIRST: signal it, wait for the thread to actually exit,
        # before anything it might still try to touch (the store) closes.
        self._stop_private_ws()
        # Close the session record BEFORE the store closes, so SESSION_END has
        # somewhere to land. It is best-effort either way.
        try:
            self.session.close()
        except Exception:  # noqa: BLE001
            logger.warning("session log could not be closed", exc_info=True)
        try:
            self.engine.shutdown(cancel_protective=False)
        except Exception:  # noqa: BLE001
            logger.exception("engine shutdown failed")
        # Released only after the engine's last writes, so the single-writer
        # assertion still guards them.
        try:
            self.store.release_writer()
        except Exception:  # noqa: BLE001
            pass
        if self._http is not None:
            try:
                self._http.shutdown()
            except Exception:  # noqa: BLE001
                pass
        try:
            self.store.close()
        except Exception:  # noqa: BLE001
            pass
        logger.info("shutdown complete")

    def stop(self) -> None:
        self._stop.set()


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def carry_orders_permitted(cfg: Any) -> Tuple[bool, str]:
    """May the carry book send an order right now? The ONE answer.

    `build_bot` hands this (bound to its config) to `CarryBroker` as the order
    gate, asked before EVERY order. The read-only preflight asks the same
    function so its "order authorisation" line is the real gate's verdict, not
    a copy of its logic.

    PAPER_TRADING refuses (the carry book has no paper venue). Testnet and demo
    are both sandboxes that cannot touch real money, so both are permitted, with
    the reason naming which one. Mainnet needs the live authorisation AND a
    signed promotion gate.
    """
    if bool(cfg.PAPER_TRADING):
        return (False, "PAPER_TRADING=1: the carry book has no paper venue")
    if bool(cfg.USE_TESTNET):
        # Testnet and DEMO are both sandboxes and neither can touch real money,
        # so both are permitted here — but the reason NAMES which one, so no log
        # line and no drill transcript can be read as mainnet evidence (0046).
        return (True, str(getattr(cfg, "BYBIT_VENUE", "testnet")).upper())
    armed, why = _config.is_live_authorized(cfg)
    if armed is not True:
        return (False, f"mainnet URL without live authorisation: {why}")
    # Defence in depth: start() already refuses a live-armed process whose
    # promotion gate is unsigned. The broker asks again, per order, so no path
    # around start() reaches a mainnet carry order.
    import promotion_gate as _gate
    if _gate.promotion_gate_allows_live(config=cfg) is not True:
        return (False, "mainnet URL: the promotion gate is not signed")
    return (True, "LIVE (authorised, promotion gate signed)")


def carry_orders_permitted_for(cfg: Any, client: Any) -> Tuple[bool, str]:
    """`carry_orders_permitted`, plus: the client must talk to the venue the
    config says it talks to.

    The gate above reads flags. The URL is what orders actually travel to. A
    client built (or injected) against mainnet under a sandbox config would
    pass every flag check and still send a real order, so the one direction
    that matters is refused here, per order: a sandbox config with a client
    whose base URL is the mainnet host.
    """
    allowed, why = carry_orders_permitted(cfg)
    if allowed is not True:
        return (allowed, why)
    if bool(cfg.USE_TESTNET):
        from bybit_connection import MAINNET_REST
        url = str(getattr(client, "base_url", "") or "").rstrip("/")
        if url == MAINNET_REST:
            return (False, f"sandbox config (USE_TESTNET=1) but the client "
                           f"targets the mainnet host {MAINNET_REST}")
        if not url:
            return (False, "sandbox config (USE_TESTNET=1) but the client "
                           "states no host: the venue it talks to is unknown")
    return (True, why)


def carry_entries_permitted_from(store: Any) -> Callable[[], Tuple[bool, str]]:
    """The carry broker's entry gate: an engaged or unreadable kill switch
    refuses every new entry. Read fresh on each order; exits never ask it."""
    def gate() -> Tuple[bool, str]:
        try:
            engaged, why = store.is_kill_switch_engaged()
        except Exception as exc:  # noqa: BLE001 - unreadable is closed
            return (False, f"KILL_SWITCH_UNREADABLE ({type(exc).__name__})")
        if engaged:
            return (False, f"KILL_SWITCH_ENGAGED: {why}")
        return (True, "kill switch clear")
    return gate


def build_bot(attach_strategy: Optional[bool] = None, **overrides: Any) -> TradingBot:
    """Construct the stack in dependency order.

    ``attach_strategy`` wires the repaired technical-analysis layer in as the
    signal source. It is a parameter rather than an assumption so a deployment
    can deliberately run the bot with **no** strategy — in which case it
    executes its loop, keeps state reconciled, serves health, and takes no
    trades. That is a legitimate and safe configuration, and it is what the
    legacy code should have done instead of fabricating BUY signals at
    confidence 0.66 when the strategy returned HOLD.

    Left at ``None`` — which is what ``main()`` passes — the choice comes from
    ``ENTRIES_ENABLED`` in config, so an operator can select strategy-neutral
    operation from the environment instead of only from Python. An explicit
    ``True``/``False`` still wins, because the tests that predate this key pass
    one and must keep meaning what they meant.

    ``ENTRIES_ENABLED`` is an **operator control, not a safety gate**. The gates
    are the kill switch, the live-arming chain and the risk gate set; running
    with entries disabled does not weaken any of them and is not a substitute
    for any of them.
    """
    cfg = overrides.pop("config", None) or _config.get_config_object()
    _config.setup_logging(cfg)
    if attach_strategy is None:
        attach_strategy = bool(getattr(cfg, "ENTRIES_ENABLED", True))
        if not attach_strategy:
            logger.warning(
                "ENTRIES_ENABLED is false: no signal source will be attached. "
                "The loop, reconciliation, stop management and health stay "
                "live; no new entry will be proposed."
            )
    bot = TradingBot(config=cfg, **overrides)
    # No exposure-increasing order may leave the client while the kill switch
    # is engaged or unreadable. Exits are never asked (see BybitClient).
    bot.client.entry_gate = carry_entries_permitted_from(bot.store)

    # ---- BOOK_MODE -------------------------------------------------------
    # "carry"       delta-neutral: long spot + short perp, collect funding.
    # "directional" the legacy technical voter.
    #
    # These are mutually exclusive and the exclusion is enforced here, at the
    # only place a book gets attached, rather than trusted to the operator. A
    # process holding a hedged pair AND a directional position shares one
    # liquidation price between two strategies that do not know about each
    # other.
    #
    # The directional voter's own Stage-1 verdict is ABSENT (M1/M2 76.1/77.5).
    # It remains the default only because dozens of tests predate the carry
    # book and assert its presence; selecting it in production is a decision an
    # operator has to make on purpose, and it is logged as one.
    # BOOK_MODE is read with a default and the notional cap is not, which is a
    # deliberate distinction rather than an inconsistency:
    #
    #   a MODE SELECTOR absent from a config object means nobody asked for the
    #   carry book, and falling back to the legacy path is the conservative
    #   reading. Test doubles across this suite pass minimal config stubs and
    #   must keep working.
    #
    #   a RISK LIMIT absent from a config object means nobody set the limit, and
    #   inventing one is how a $100 cap silently becomes whatever the literal in
    #   the source happens to say. The cap comes from
    #   shadow.SHADOW_MAX_NOTIONAL_USD, the same constant promotion_gate polices,
    #   and there is no fallback.
    #
    # An unrecognised mode still raises: a typo must not select a strategy.
    book_mode = str(getattr(cfg, "BOOK_MODE", "directional") or "directional").lower()
    if book_mode not in ("carry", "directional", "shadow"):
        raise ValueError(
            f"BOOK_MODE={cfg.BOOK_MODE!r} is not a book. Refusing rather than "
            "falling back: a typo must not silently select a strategy.")
    if book_mode == "carry" and attach_strategy and bot.carry is None:
        from carry_broker import CarryBroker
        from carry_engine import CarryEngine

        # The notional cap comes from shadow.SHADOW_MAX_NOTIONAL_USD, which is
        # the SAME constant promotion_gate re-derives its cap check from. It is
        # not read from config and there is no default here.
        #
        # The first version of this wiring read `getattr(cfg, "MAX_NOTIONAL_USD",
        # 100.0)` from a key that DOES NOT EXIST in Config. It therefore
        # silently invented a risk limit that happened to be right, and would
        # have gone on inventing it if anyone moved the real cap. A risk
        # parameter with a silent default is not a risk parameter.
        #
        # `next_order_seq` and `trip_kill_switch` are called directly and are
        # NOT guarded by hasattr. The earlier version guarded them, and because
        # both names were wrong the guard swallowed it: every order would have
        # received sequence 0, so the same intent on a later candle would build
        # the SAME orderLinkId, the venue would reject it as a duplicate, and
        # the adapter would resolve the duplicate by returning the OLD fill. The
        # book would believe it had re-opened a position it never placed.
        # An AttributeError at construction is the correct outcome for a
        # misnamed dependency.
        import shadow as _shadow

        # FINANCING (0033, INVENTORY D7). Read with no default and refused when
        # absent: the engine used to fall back to its own constructor default
        # of 5%, so an overlay on BTC the client already owns was gated as a
        # financed book, or the reverse, with nothing in the log to say which.
        borrow_apr = cfg.CARRY_BORROW_APR
        if borrow_apr is None:
            raise ValueError(
                "BOOK_MODE=carry requires CARRY_BORROW_APR (a fraction per "
                "year: 0.0 = an overlay on BTC already owned, 0.05 = financed "
                "at 5%). It decides whether the carry clears its cost of "
                "capital; refusing rather than assuming.")

        # WHO MAY SEND A CARRY ORDER (0033, INVENTORY D2). The carry broker
        # bypasses TradingEngine, so PAPER_TRADING never reached it: with
        # USE_TESTNET=0 PAPER_TRADING=1 the process called itself SANDBOX
        # (PAPER) and the carry broker would still have POSTed to mainnet.
        # Asked before EVERY order, read fresh each time.
        def _carry_orders_permitted():
            return carry_orders_permitted_for(cfg, bot.client)

        # HOW THE LONG SIDE IS HELD (0036). No default: "overlay" shorts the
        # perp against BTC the client already owns and never touches their
        # spot; "acquire" buys the spot leg and sells it again. PHASE1_DECISION
        # chose the overlay as the product, and the engine went on buying spot
        # because nothing made the choice explicit.
        execution_mode = cfg.CARRY_EXECUTION_MODE
        if execution_mode is None:
            raise ValueError(
                "BOOK_MODE=carry requires CARRY_EXECUTION_MODE (\"overlay\" = "
                "hedge BTC the client already owns, never trading their spot; "
                "\"acquire\" = buy the spot leg and sell it again). The two "
                "place different orders; refusing rather than assuming.")

        # THE LEDGER (0044). Wrapped around the venue adapter, not called by
        # the engine: the broker is the engine's ONLY path to an order, so
        # every leg is booked and no code path anyone adds later can forget
        # to. The journal is loaded from the store here and written back by
        # tick(); `Journal.from_rows` re-validates every entry's balance on
        # load, so a database edited by hand fails at startup rather than in
        # a statement.
        import ledger as _ledger

        _rows = bot.store.load_ledger()
        bot.carry_journal = (_ledger.Journal.from_rows(_rows) if _rows
                             else _ledger.Journal())
        if _rows:
            logger.warning(
                "carry ledger: resumed %d entries, realised net $%.2f",
                len(bot.carry_journal.entries),
                bot.carry_journal.statement()["net_usd"])

        bot.carry = CarryEngine(
            broker=_ledger.LedgerBroker(
                CarryBroker(
                    client=bot.client,
                    sequence_source=lambda product, symbol, purpose:
                        bot.store.next_order_seq(),
                    session_token=bot.store.session_boot_token(),
                    order_gate=_carry_orders_permitted,
                    entry_gate=carry_entries_permitted_from(bot.store)),
                bot.carry_journal,
                ms=lambda: int(time.time() * 1000),
                spot_symbol=cfg.CARRY_SPOT_SYMBOL),
            kill_switch=bot.store.trip_kill_switch,
            spot_symbol=cfg.CARRY_SPOT_SYMBOL,
            perp_symbol=cfg.CARRY_PERP_SYMBOL,
            max_notional_usd=float(_shadow.SHADOW_MAX_NOTIONAL_USD),
            borrow_apr=float(borrow_apr),
            execution_mode=execution_mode,
            # HOW each leg is executed (0040). Entries may rest at the touch
            # under "maker_first"; exits cross under both. Defaulted to
            # "taker" — the round trip the entry gate prices.
            execution_style=cfg.CARRY_EXECUTION_STYLE,
            maker_wait_s=float(cfg.CARRY_MAKER_WAIT_S),
            # F4 (0048). The live book measures how far the price can move
            # before the short is liquidated, and halts if the venue will not
            # say. Off by default in the constructor so test doubles that were
            # never asked to model a liquidation price are unaffected; ON
            # here, because this is the one that loses the position.
            require_liquidation_check=True,
            # What that gate reads each held tick is kept (additive table, no
            # new venue request) so a later replay can run on the venue's own
            # numbers. A store without the method records nothing and says so.
            risk_observer=getattr(bot.store, "record_risk_observation", None),
            # THE LEDGER (0037). Called by the engine on every state change,
            # straight through to the store. Not a hasattr guard and not a
            # lambda that swallows: a store without this method is a
            # misconfiguration that must fail at construction.
            persist=bot._persist_carry,
        )
        # The pair gate. Until now max_notional_usd was the ONLY size control on
        # the carry book: the legs passed through no RiskManager at all. This is
        # the pair-shaped equivalent — kill switch, one-pair, cap, margin floor,
        # entries per day — consulted before both legs, never after one.
        from carry_risk import CarryRisk

        bot.carry.pair_risk = CarryRisk(
            store=bot.store,
            max_notional_usd=float(_shadow.SHADOW_MAX_NOTIONAL_USD))
        logger.warning(
            "BOOK_MODE=carry: delta-neutral book attached, cap $%.2f, borrow "
            "%.4f/yr, execution %s/%s, orders: %s. The directional strategy is "
            "NOT attached and will not be consulted.",
            bot.carry.max_notional_usd, bot.carry.borrow_apr,
            bot.carry.execution_mode, bot.carry.execution_style,
            _carry_orders_permitted()[1])
        return bot

    if book_mode == "shadow" and attach_strategy and bot.strategy is None:
        # ROADMAP Stage A item 2. Until now `build_bot` attached
        # `MarketStrategy` unconditionally — the legacy technical voter, whose
        # Stage-1 verdict is ABSENT. The one rule this programme has ever
        # cleared was wired into nothing, and `project_status` said so in as
        # many words: "nothing wires that signal into the engine and no trading
        # path consults the field. Wiring it up is a human decision that has
        # not been taken."
        #
        # This is that wiring, and it changes what the shell RUNS, not what it
        # is allowed to do: paper only, BTCUSDT only, one position, one entry
        # per calendar day, 100.00 USD notional, every existing gate unchanged,
        # and the promotion gate still 2 of 8.
        # `import shadow as _shadow` lives inside the CARRY branch above, so
        # it is not in scope here. Imported again rather than hoisted: the two
        # branches are mutually exclusive and neither should pay for the
        # other's imports.
        # The rule trades a USDT-margined LINEAR perpetual and was measured on
        # Binance linear daily bars. `get_klines` reads whatever category the
        # process is configured for, so a spot-configured shell would feed the
        # cleared rule SPOT bars and call the result the same rule. BTC spot
        # and perp closes are near enough to make that invisible, which is
        # exactly why it is refused rather than warned about.
        # `import shadow as _shadow` lives inside the CARRY branch above, so
        # it is not in scope here. Imported before the category check because
        # the refusal names the signal.
        import shadow as _shadow
        from shadow_strategy import ShadowStrategy, live_providers

        _category = str(getattr(cfg, "CATEGORY", "spot")).lower()
        if _category != "linear":
            raise ValueError(
                f"BOOK_MODE=shadow requires CATEGORY=linear and this process "
                f"is {_category!r}. {_shadow.SHADOW_SIGNAL} was measured on "
                "linear daily bars, and the funding endpoint it reads does not "
                "exist on spot. Refusing rather than substituting an "
                "instrument: BTC spot and perp closes are near enough to make "
                "the substitution invisible.")

        status_provider, bar_provider, funding_provider = live_providers(
            bot.client, lookback_days=int(getattr(cfg, "KLINE_LOOKBACK", 300)))
        bot.strategy = ShadowStrategy(
            status_provider=status_provider,
            funding_provider=funding_provider,
            bar_provider=bar_provider,
        )
        # The time exit the runtime never had. Read from the frozen module so
        # it cannot drift from the rule that was measured.
        from signals import funding_carry_fade_btc_v1 as _fb

        bot.shadow_horizon_days = int(_fb.HORIZON)
        allowed, why = _shadow.shadow_is_permitted(status_provider())
        logger.warning(
            "BOOK_MODE=shadow: %s attached for %s, cap $%.2f, permitted=%s "
            "(%s). The directional voter is NOT attached. A shadow fill is "
            "not evidence of skill — it exists to detect DECAY.",
            _shadow.SHADOW_SIGNAL, _shadow.SHADOW_SYMBOL,
            _shadow.SHADOW_MAX_NOTIONAL_USD, allowed, why)
        return bot

    if attach_strategy and bot.strategy is None:
        from technical_analysis import MarketStrategy, TechnicalAnalysis

        bot.strategy = MarketStrategy(
            client=bot.client,
            analyser=TechnicalAnalysis(config=cfg),
            interval=str(getattr(cfg, "PRIMARY_TIMEFRAME", "60")),
            higher_interval=(
                list(getattr(cfg, "SECONDARY_TIMEFRAMES", ()) or ["240"])[0]
                if getattr(cfg, "SECONDARY_TIMEFRAMES", None) else "240"
            ),
            lookback=int(getattr(cfg, "KLINE_LOOKBACK", 300)),
            config=cfg,
            memory=bot.memory,
        )
        # The learned policy wraps the classical strategy rather than replacing
        # it. In every mode the classical signal is what produces the trade's
        # geometry; the model can, at most, attach a calibrated probability to
        # it. With POLICY_MODE=off the wrapper is not even constructed, so the
        # ML path cannot affect a deployment that has not asked for it.
        if str(getattr(cfg, "POLICY_MODE", "off")).lower() != "off":
            from ml_strategy import PolicyStrategy

            wrapped = PolicyStrategy(
                bot.strategy, config=cfg, memory=bot.memory, store=bot.store,
            )
            bot.strategy = wrapped
            allowed, reason = wrapped.may_propose()
            logger.warning(
                "policy wrapper attached: mode=%s may_propose=%s (%s)",
                wrapped.mode, allowed, reason,
            )
        logger.info("strategy attached: %s", type(bot.strategy).__name__)
    return bot


def main(argv: Optional[List[str]] = None) -> int:
    """Process entry point.

    Everything this function needs is defined above it. The legacy module called
    ``main()`` from a ``__main__`` block two thirds of the way down the file, so
    every class defined below that point was undefined at call time.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        bot = build_bot()
    except Exception as exc:  # noqa: BLE001
        logger.critical("could not construct the bot: %s", exc, exc_info=True)
        return 1
    try:
        return bot.run()
    except KeyboardInterrupt:
        bot.stop()
        bot.shutdown()
        return 0


if __name__ == "__main__":
    sys.exit(main())
