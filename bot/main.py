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
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List, Optional

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


# ---------------------------------------------------------------------------
# health server
# ---------------------------------------------------------------------------


class _HealthHandler(BaseHTTPRequestHandler):
    """Answers the container health check with real state.

    Audit C18: `ENABLE_HEALTH_CHECK=true` was configured but **no health server
    existed**. If the orchestrator health-checks the port, the task is killed and
    restarted in a loop — and each restart re-entered live trading with amnesia.
    """

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
    ) -> None:
        #: The delta-neutral carry book. When present it OWNS the cycle and the
        #: directional strategy is not consulted at all — see `tick`. The two
        #: are mutually exclusive by construction, not by convention, because a
        #: process running both would hold a hedged pair AND a directional bet
        #: against the same margin.
        self.carry = carry
        self.cfg = config if config is not None else _config.get_config_object()
        self.store = store if store is not None else StateStore()
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

    def health(self) -> Dict[str, Any]:
        """Real state, not a hardcoded 200.

        Reports unhealthy when the kill switch is engaged or when any position
        is unprotected — both are conditions an operator must see.
        """
        try:
            kill_engaged, kill_reason = self.store.is_kill_switch_engaged()
            naked = [p["symbol"] for p in self.store.positions_without_stops()]
            return {
                "healthy": not kill_engaged and not naked and self._reconciled,
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
            }
        except Exception as exc:  # noqa: BLE001
            return {"healthy": False, "error": f"{type(exc).__name__}: {exc}"}

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
        self._http = HTTPServer((host, port), handler)
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
        if bool(getattr(self.cfg, "ALLOW_SHORTS", False)):
            for symbol in self.symbols:
                if not self.client.set_leverage(symbol):
                    logger.critical(
                        "could not set leverage for %s; refusing to start. A "
                        "position opened at the account's default leverage is a "
                        "position sized by something other than the risk model.",
                        symbol,
                    )
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
        # State what this process is before it does anything. Research is
        # closed and nothing has cleared Stage 1; the log must say so rather
        # than leave a reader to infer capability from silence.
        logger.warning("%s", _project_status.current(self.cfg).summary_line())
        self.session.start(reconciled=self._reconciled)
        return True

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

        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001
                # One bad cycle must not kill the process, but it must be loud
                # and it must not be silently retried at full speed.
                logger.exception("cycle failed: %s", exc)
                self._stop.wait(min(30.0, self.loop_interval_s))
                continue
            self._stop.wait(self.loop_interval_s)

        self.shutdown()
        return 0

    def tick(self) -> None:
        """One cycle: refresh equity, check the brakes, then consider each symbol."""
        self.session.tick()
        try:
            equity = self.client.get_equity()
            self.risk.update_equity(equity)
        except BybitAPIError as exc:
            logger.error("equity unreadable this cycle: %s", exc)
            return

        # Look at what we HOLD before deciding what to do. A stop or take-profit
        # that filled at the venue since the last cycle must reach the ledger
        # before any gate reads open_position_count or the loss streak.
        try:
            observed = self.engine.observe_exits()
            if observed.get("closed") or observed.get("reduced"):
                logger.warning("exits observed this cycle: %s", observed)
        except Exception as exc:  # noqa: BLE001
            logger.exception("observe_exits failed; continuing: %s", exc)

        if self.risk.should_halt_trading():
            logger.warning("trading halted by the risk layer this cycle")
            return

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
            symbol = self.symbols[0] if self.symbols else "BTCUSDT"
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
                return
            mark, spot = view.perp_mark, view.spot_mark
            funding_bps = view.funding_bps
            self.carry.snapshot = view
            decision = self.carry.on_candle(
                mark=mark, funding_bps=funding_bps, spot=spot,
                timestamp_ms=int(time.time() * 1000))
            logger.info(
                "carry: %s (%s) state=%s perp=%.2f spot=%.2f "
                "basis=%.1fbps funding=%.3fbps %s",
                decision.action, decision.reason, decision.state.value,
                mark, spot, (mark / spot - 1.0) * 1e4, funding_bps,
                decision.detail or "")
            if decision.state.name == "HALTED":
                logger.critical(
                    "carry book HALTED — a human must clear it: %s",
                    decision.reason)
            return

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
                intent = self.strategy.signal_for(symbol)
            except Exception as exc:  # noqa: BLE001
                logger.exception("strategy failed for %s: %s", symbol, exc)
                continue
            if intent is None:
                continue
            report = self.engine.execute(intent)
            logger.info("%s -> %s (%s)", symbol, report.reason, report.stage)

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
        # Close the session record BEFORE the store closes, so SESSION_END has
        # somewhere to land. It is best-effort either way.
        try:
            self.session.close()
        except Exception:  # noqa: BLE001
            logger.warning("session log could not be closed", exc_info=True)
        try:
            self.store.release_writer()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.engine.shutdown(cancel_protective=False)
        except Exception:  # noqa: BLE001
            logger.exception("engine shutdown failed")
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
    if book_mode not in ("carry", "directional"):
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
            if bool(cfg.PAPER_TRADING):
                return (False, "PAPER_TRADING=1: the carry book has no paper "
                               "venue")
            if bool(cfg.USE_TESTNET):
                return (True, "TESTNET")
            armed, why = _config.is_live_authorized(cfg)
            if armed is not True:
                return (False, f"mainnet URL without live authorisation: {why}")
            # Defence in depth: start() already refuses a live-armed process
            # whose promotion gate is unsigned. The broker asks again, per
            # order, so no path around start() reaches a mainnet carry order.
            import promotion_gate as _gate
            if _gate.promotion_gate_allows_live(config=cfg) is not True:
                return (False, "mainnet URL: the promotion gate is not signed")
            return (True, "LIVE (authorised, promotion gate signed)")

        bot.carry = CarryEngine(
            broker=CarryBroker(
                client=bot.client,
                sequence_source=lambda product, symbol, purpose:
                    bot.store.next_order_seq(),
                order_gate=_carry_orders_permitted),
            kill_switch=bot.store.trip_kill_switch,
            spot_symbol=cfg.CARRY_SPOT_SYMBOL,
            perp_symbol=cfg.CARRY_PERP_SYMBOL,
            max_notional_usd=float(_shadow.SHADOW_MAX_NOTIONAL_USD),
            borrow_apr=float(borrow_apr),
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
            "%.4f/yr, orders: %s. The directional strategy is NOT attached "
            "and will not be consulted.",
            bot.carry.max_notional_usd, bot.carry.borrow_apr,
            _carry_orders_permitted()[1])
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
