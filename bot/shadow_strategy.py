"""The signal source that lets the paper shell shadow the one cleared rule.

WHY THIS IS A STRATEGY AND NOT A PARALLEL UNIVERSE
==================================================
The obvious way to build a shadow pilot is a separate loop with its own fills,
its own accounting and its own idea of what a position is. That is also the way
it silently drifts from the shell that would eventually trade it, so that by the
time anyone compares them they are measuring two different systems.

So this is a `signal_for(symbol)` provider — the same interface
`technical_analysis.MarketStrategy` implements and the same one `TradingBot`
already calls. Every existing gate, the kill switch, the risk chain, the
protective-stop guarantee and the paper simulator apply unchanged, because this
does not go around any of them. What it adds is on top: the shadow caps.

WHAT IT REFUSES
===============
* it produces nothing unless `ProjectStatus.cleared_edge_signal` names
  `funding_carry_fade_btc_v1` and a human has not revoked it;
* it produces nothing for any symbol but BTCUSDT;
* it produces nothing when the shadow caps refuse — one position, one entry per
  calendar day, 100.00 USD notional;
* it produces nothing when the monitors are in ALERT;
* it never sets `win_probability`. That field belongs to a promoted model and
  no model exists;
* it computes no barrier of its own. The stop is `STOP_ATR` ATRs from entry and
  the target is `TAKE_PROFIT_R` R, both read from the frozen signal module —
  the same geometry the cleared measurement used.

A NOTE ON WHAT A SHADOW FILL IS WORTH
=====================================
Nothing, as evidence of skill. The cleared rule was measured against a rotation
null and a drift-controlled contrast on 41 out-of-sample trades; a shadow fill
is one trade with no null behind it. Shadow exists to detect **decay** — to
notice when the rule stops behaving like the rule that was measured — not to
re-establish that it worked. `EDGE.md` §41h says so and the artefact repeats it.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, List, Optional, Sequence

import shadow
from signals import funding_carry_fade_btc_v1 as signal_module
from trading_engine import TradeIntent

logger = logging.getLogger(__name__)

__all__ = ["ShadowStrategy"]


class ShadowStrategy:
    """Proposes at most one simulated entry for the cleared signal.

    Constructed with everything it needs; no globals, no clock reads inside the
    decision path. `now_utc` is injected so the day-cap is testable without
    freezing time, and so two runs over the same inputs give the same answer.
    """

    #: The identity the live promotion check compares against
    #: ``ProjectStatus.cleared_edge_signal``. Only a strategy that declares the
    #: cleared name may be attached to a live-armed process (main.py).
    signal_name = shadow.SHADOW_SIGNAL

    def __init__(self, *, status_provider, funding_provider, bar_provider,
                 caps: Optional[shadow.ShadowCaps] = None,
                 closed_trades: Optional[List[shadow.ShadowTrade]] = None,
                 now_utc=None) -> None:
        self._status = status_provider
        self._funding = funding_provider
        self._bars = bar_provider
        self.caps = caps or shadow.ShadowCaps()
        self.closed_trades: List[shadow.ShadowTrade] = list(closed_trades or [])
        self._now = now_utc or (lambda: dt.datetime.now(dt.timezone.utc))
        self.last_refusal: str = "not yet evaluated"

    # -- the gate chain, in order --------------------------------------------

    def _refuse(self, reason: str) -> None:
        self.last_refusal = reason
        logger.info("shadow: no intent (%s)", reason)

    def monitor_readings(self) -> List[shadow.MonitorReading]:
        return shadow.evaluate_monitors(self.closed_trades)

    def signal_for(self, symbol: str) -> Optional[TradeIntent]:
        """A `TradeIntent`, or `None`. Never raises into the trading loop.

        The order of the checks is the order of their authority: scope first
        (is there anything to shadow at all), then monitors (has it stopped
        behaving), then caps (would this breach a frozen limit), then the rule
        itself. A cheaper check placed after an expensive one would still be
        correct; a *weaker* check placed after a stronger one would not be, and
        this order puts the strongest first.
        """
        allowed, why = shadow.shadow_is_permitted(self._status())
        if not allowed:
            self._refuse(why)
            return None

        if symbol != shadow.SHADOW_SYMBOL:
            self._refuse(f"{symbol} is not {shadow.SHADOW_SYMBOL}")
            return None

        readings = self.monitor_readings()
        if shadow.entries_blocked_by_monitor(readings):
            breached = [r.name for r in readings if r.state == "ALERT"]
            self._refuse(
                f"monitor ALERT ({', '.join(breached)}); entries blocked. The "
                f"signal is NOT retuned and the research clear is NOT touched "
                f"— a human decides both.")
            return None

        now = self._now()
        day = now.date().isoformat()
        blocked = self.caps.why_blocked(
            symbol=symbol, day=day,
            notional_usd=shadow.SHADOW_MAX_NOTIONAL_USD)
        if blocked is not None:
            self._refuse(f"shadow cap: {blocked}")
            return None

        setup = self._current_setup(symbol)
        if setup is None:
            self._refuse("no funding setup at the last closed bar")
            return None
        direction, entry_price, atr = setup

        risk = shadow.SHADOW_MAX_NOTIONAL_USD  # notional, not risk capital
        if not (entry_price > 0.0 and atr > 0.0):
            self._refuse("entry price or ATR unusable")
            return None

        stop_distance = signal_module.STOP_ATR * atr
        if direction == signal_module.SHORT_SETUP:
            side, stop = "SELL", entry_price + stop_distance
            target = entry_price - signal_module.TAKE_PROFIT_R * stop_distance
        else:
            side, stop = "BUY", entry_price - stop_distance
            target = entry_price + signal_module.TAKE_PROFIT_R * stop_distance

        self.last_refusal = ""
        return TradeIntent(
            symbol=symbol,
            signal_type=side,
            entry_price=float(entry_price),
            stop_price=float(stop),
            take_profits=((float(target), 1.0),),
            confidence=None,
            # NEVER set. This field belongs to a promoted model and no model
            # exists; filling it here would be inventing a calibration.
            win_probability=None,
            reason=(
                f"SHADOW {shadow.SHADOW_SIGNAL} {direction} — research clear "
                f"only, paper, capped at {risk:.2f} USD notional. Not a live "
                f"order and not evidence of skill."),
        )

    # -- the rule, read from the frozen module -------------------------------

    def _current_setup(self, symbol: str):
        """(direction, entry_price, atr) at the last closed bar, or None."""
        try:
            bars: Sequence[Any] = self._bars(symbol)
            funding = self._funding(symbol)
        except Exception as exc:  # noqa: BLE001
            logger.warning("shadow: inputs unavailable (%s)", exc)
            return None
        if not bars or len(bars) < signal_module.ATR_PERIOD + 2:
            return None

        decision_index = len(bars) - 2      # the last CLOSED decision bar
        setups = signal_module.funding_setups(bars, funding)
        direction = setups.get(decision_index)
        if direction is None:
            return None

        # ONE TRADE PER CONTIGUOUS RUN — slice 17's rule, and the rule the
        # cleared measurement was scored under. Expressed causally: enter only
        # on the FIRST bar of a run of setups, i.e. when the previous decision
        # bar was not a setup.
        #
        # This was WRONG in the first version of this module, and the STEP-4
        # replay is what caught it. Without this test the shadow entered
        # mid-run as well, taking 55 trades on the out-of-sample window where
        # the cleared rule schedules 41 — piloting a COUSIN of the rule rather
        # than the rule. Rich funding persists for days, so the two are not
        # nearly the same object: the replay's mean net R came out NEGATIVE
        # while the rule it was supposed to be shadowing scored +0.1736.
        #
        # A monitor watching the wrong object is worse than no monitor, because
        # it looks like diligence.
        if setups.get(decision_index - 1) is not None:
            return None

        entry_price = float(bars[decision_index + 1].open)   # next_open
        atr = _wilder_atr_last(bars[:decision_index + 1],
                               signal_module.ATR_PERIOD)
        if atr is None:
            return None
        return direction, entry_price, atr

    # -- bookkeeping the shell drives ---------------------------------------

    def record_fill(self, *, day: str) -> None:
        self.caps.record_entry(day=day)

    def record_close(self, trade: shadow.ShadowTrade) -> None:
        self.caps.record_exit()
        self.closed_trades.append(trade)

    def snapshot(self) -> dict:
        """What the health payload reports. No PnL field, by construction."""
        readings = self.monitor_readings()
        return {
            "shadow_signal": shadow.SHADOW_SIGNAL,
            "shadow_symbol": shadow.SHADOW_SYMBOL,
            "closed_shadow_trades": len(self.closed_trades),
            "open_shadow_positions": self.caps.open_positions,
            "monitor_status": shadow.monitor_status(readings),
            "entries_blocked_by_monitor":
                shadow.entries_blocked_by_monitor(readings),
            "monitors": [
                {"name": r.name, "state": r.state, "value": r.value,
                 "threshold": r.threshold, "n_trades": r.n_trades}
                for r in readings
            ],
            "caps": {
                "max_concurrent_positions": self.caps.max_concurrent_positions,
                "max_entries_per_day": self.caps.max_entries_per_day,
                "max_notional_usd": self.caps.max_notional_usd,
            },
            "last_refusal": self.last_refusal,
        }


def _wilder_atr_last(bars: Sequence[Any], period: int) -> Optional[float]:
    """Wilder ATR at the final bar, or None.

    Reimplemented in miniature rather than imported from `tools/`, because
    `tools/` is not an installed package and this module must not depend on it
    — the same reason `project_status` duplicates the bars. A test asserts this
    agrees with `sweep_geometry.wilder_atr` on real data, so the duplication
    cannot drift silently.
    """
    if len(bars) < period + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        high, low = float(bars[i].high), float(bars[i].low)
        prev_close = float(bars[i - 1].close)
        trs.append(max(high - low, abs(high - prev_close),
                       abs(low - prev_close)))
    if len(trs) < period:
        return None
    atr = sum(trs[:period]) / float(period)
    for value in trs[period:]:
        atr = (atr * (period - 1) + value) / float(period)
    return atr
