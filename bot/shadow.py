"""Constrained shadow pilot for the one cleared signal. Glass cockpit, not a claim.

WHAT THIS MODULE IS FOR
=======================
Slice 57 cleared `funding_carry_fade_btc_v1` under a pre-declared out-of-sample
gate, and the clear is thin: 41 trades, M1 95.13 against a bar of 95.0, the most
recent half of the window flat at +0.0236, three trades carrying 41% of the net
R, and the whole thing measured on a time split of data that had already been
looked at.

After a result like that a bank pilots under glass and watches it decay. This
module is the glass. It lets the execution shell simulate the cleared rule under
caps fixed in advance, and it computes the monitors that say when the rule has
stopped behaving like the rule that was measured.

**It does not trade live, cannot be made to, and adds no path toward it.**

THE THREE THINGS IT ENFORCES
============================
1. **Scope.** Only `funding_carry_fade_btc_v1`, only BTCUSDT, and only while
   `ProjectStatus.cleared_edge_signal` actually names it. If the flag is null or
   names something else, :func:`shadow_is_permitted` says no and no intent is
   produced. A shadow that runs on a revoked flag is a shadow of nothing.
2. **Caps**, frozen in EDGE.md §41d before this file existed: one concurrent
   position, one entry per calendar day, 100.00 USD notional, one symbol. They
   are *additional* to every existing gate and replace none of them.
3. **Monitors**, thresholds frozen in EDGE.md §41e before any fill was
   simulated. They are pure functions of a list of closed trades so they can be
   tested against hand-built inputs, and they raise status — they never trade.

WHAT IT DELIBERATELY CANNOT DO
==============================
* size above the cap, or grow the cap with equity — the notional is a fixed
  dollar figure for the reason §41d gives: a fraction authorises more risk the
  better things go, which is backwards for a pilot;
* clear a kill switch, or weaken a risk gate;
* change a constant. It imports every one from the signal module and declares
  none of its own;
* revoke the research clear. A monitor breach blocks entries; withdrawing a
  research claim is a human act with its own acknowledgement string;
* write a PnL or win-rate field anywhere. `session_log.FORBIDDEN_FIELD_MARKERS`
  still refuses one, and a shadow pilot reporting an equity curve would be a
  production profit claim wearing a research label.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from signals import funding_carry_fade_btc_v1 as _signal

logger = logging.getLogger(__name__)

__all__ = [
    "SHADOW_SIGNAL",
    "SHADOW_SYMBOL",
    "SHADOW_MAX_CONCURRENT_POSITIONS",
    "SHADOW_MAX_ENTRIES_PER_DAY",
    "SHADOW_MAX_NOTIONAL_USD",
    "CONSTANTS_FINGERPRINT",
    "MONITOR_THRESHOLDS",
    "REVOCATION_ACK",
    "REVOCATIONS_PATH",
    "ShadowTrade",
    "ShadowCaps",
    "MonitorReading",
    "shadow_is_permitted",
    "constants_fingerprint",
    "rolling_mean_r",
    "rolling_mean_r_by_days",
    "concentration_top3",
    "halves_split",
    "evaluate_monitors",
    "monitor_status",
    "entries_blocked_by_monitor",
    "read_revocations",
    "is_revoked",
    "revoke_cleared_edge",
]

# ---------------------------------------------------------------------------
# scope
# ---------------------------------------------------------------------------

#: The ONLY signal this module will shadow, and the only symbol.
SHADOW_SIGNAL = _signal.NAME
SHADOW_SYMBOL = "BTCUSDT"

# ---------------------------------------------------------------------------
# caps — EDGE.md §41d, frozen before this file existed
# ---------------------------------------------------------------------------

SHADOW_MAX_CONCURRENT_POSITIONS = 1
SHADOW_MAX_ENTRIES_PER_DAY = 1

#: A FIXED dollar figure, deliberately not a fraction of equity. §41d: a
#: percentage cap grows with the account, so it silently authorises more risk
#: the better things go — the wrong direction for a pilot whose purpose is to
#: find out whether the rule works at all.
SHADOW_MAX_NOTIONAL_USD = 100.00


def constants_fingerprint() -> str:
    """sha256 of the signal's frozen constants. Changes if any of them does."""
    blob = json.dumps(_signal.CONSTANTS, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


#: Pinned in EDGE.md §41c. A test asserts this, so a constant edited anywhere
#: in the signal module turns this module red rather than silently shadowing a
#: different rule from the one that was cleared.
CONSTANTS_FINGERPRINT = "662de0115880871352d5d623b1020eaa"

# ---------------------------------------------------------------------------
# monitor thresholds — EDGE.md §41e, frozen before any fill was simulated
# ---------------------------------------------------------------------------

#: Every number here was written into EDGE.md §41e before `shadow.py` existed,
#: with its derivation. `-0.25` is the worst full-sample fold this rule has
#: produced (-0.2124) rounded away from zero; `0.60` is half again the OOS
#: sample's own concentration of 0.41; `K = 10` and M-2's 5-trade floor come
#: from a rule that produced 41 trades in two years.
#:
#: **None of these may be changed after a replay has been run.** A threshold
#: fitted to observed shadow PnL is not a monitor, it is a description.
MONITOR_THRESHOLDS: Dict[str, Any] = {
    "M1_rolling_trades": {
        "k": 10, "warn_below": 0.0, "alert_below": -0.25, "min_trades": 10,
    },
    "M2_rolling_days": {
        "window_days": 90, "warn_below": 0.0, "alert_below": -0.25,
        "min_trades": 5,
    },
    "M3_concentration": {
        "top_n": 3, "warn_above": 0.60, "min_trades": 10,
    },
    "M4_halves": {
        "min_trades": 20,
    },
}

# ---------------------------------------------------------------------------
# revocation — EDGE.md §41f
# ---------------------------------------------------------------------------

#: Not derivable from config, exactly as the kill switch's clear token is not.
#: A human types this or nothing happens.
REVOCATION_ACK = "HUMAN_REVOKED_CLEARED_EDGE"

REVOCATIONS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "artifacts",
    "edge_revocations.json")


# ---------------------------------------------------------------------------
# data
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ShadowTrade:
    """One CLOSED simulated trade. Frozen: a monitor input that can be mutated
    between being computed and being reported is a monitor input that can be
    made to say something untrue."""

    entry_utc: str          # ISO 8601, UTC
    exit_utc: str
    direction: str          # LONG_SETUP | SHORT_SETUP
    net_r: float
    notional_usd: float

    def entry_date(self) -> dt.date:
        return dt.datetime.fromisoformat(
            self.entry_utc.replace("Z", "+00:00")).date()

    def exit_date(self) -> dt.date:
        return dt.datetime.fromisoformat(
            self.exit_utc.replace("Z", "+00:00")).date()


@dataclass(frozen=True)
class MonitorReading:
    """One monitor's answer. `state` is one of INSUFFICIENT_DATA / OK / WARN /
    ALERT."""

    name: str
    state: str
    value: Optional[float]
    threshold: Optional[float]
    n_trades: int
    detail: str = ""


@dataclass
class ShadowCaps:
    """The frozen caps, and the running state needed to enforce them.

    A dataclass rather than loose module state so a test can construct one, and
    so two bots in one process cannot share a counter by accident.
    """

    max_concurrent_positions: int = SHADOW_MAX_CONCURRENT_POSITIONS
    max_entries_per_day: int = SHADOW_MAX_ENTRIES_PER_DAY
    max_notional_usd: float = SHADOW_MAX_NOTIONAL_USD
    symbol: str = SHADOW_SYMBOL
    open_positions: int = 0
    entries_by_day: Dict[str, int] = field(default_factory=dict)

    def why_blocked(self, *, symbol: str, day: str,
                    notional_usd: float) -> Optional[str]:
        """The FIRST cap that refuses this entry, or None if all of them allow it.

        Returns a reason string rather than a bool so a refusal can be logged
        with its cause. An operator reading a shadow log should not have to
        guess which cap bound.
        """
        if symbol != self.symbol:
            return (f"shadow is {self.symbol}-only and was asked for {symbol!r}")
        if self.open_positions >= self.max_concurrent_positions:
            return (f"{self.open_positions} position(s) already open, cap is "
                    f"{self.max_concurrent_positions}")
        taken = self.entries_by_day.get(day, 0)
        if taken >= self.max_entries_per_day:
            return (f"{taken} entry/entries already taken on {day}, cap is "
                    f"{self.max_entries_per_day} per calendar day")
        if not (notional_usd > 0.0):
            return f"notional {notional_usd!r} is not positive"
        if notional_usd > self.max_notional_usd:
            return (f"notional {notional_usd:.2f} USD exceeds the frozen cap of "
                    f"{self.max_notional_usd:.2f} USD")
        return None

    def record_entry(self, *, day: str) -> None:
        self.entries_by_day[day] = self.entries_by_day.get(day, 0) + 1
        self.open_positions += 1

    def record_exit(self) -> None:
        self.open_positions = max(0, self.open_positions - 1)


# ---------------------------------------------------------------------------
# scope gate
# ---------------------------------------------------------------------------


def shadow_is_permitted(status: Any, *,
                        revocations_path: Optional[str] = None
                        ) -> Tuple[bool, str]:
    """May the shell shadow this signal right now? Returns (allowed, reason).

    Fails closed on every ambiguity. Four conditions, all required:

    * the research flag names exactly this signal — a shadow running while the
      flag is null or names something else is a shadow of nothing;
    * the signal has not been revoked by a human;
    * execution mode is paper (shadow is a paper activity by construction);
    * live is not authorised. This is belt to the braces: the live-arming chain
      does not consult this function, and this function refuses anyway.
    """
    cleared = getattr(status, "cleared_edge_signal", None)
    if cleared != SHADOW_SIGNAL:
        return False, (
            f"cleared_edge_signal is {cleared!r}, not {SHADOW_SIGNAL!r}; there "
            f"is nothing for the shadow path to simulate")
    if is_revoked(SHADOW_SIGNAL, path=revocations_path):
        return False, (
            f"{SHADOW_SIGNAL} has been revoked by a human; the research clear "
            f"no longer stands")
    mode = str(getattr(status, "execution_mode", "")).lower()
    if mode != "paper":
        return False, f"execution_mode is {mode!r}; shadow requires paper"
    if bool(getattr(status, "live_authorized", False)):
        return False, (
            "live is authorised; the shadow path refuses to run beside an armed "
            "shell")
    return True, (
        f"shadowing {SHADOW_SIGNAL} on {SHADOW_SYMBOL} in paper mode under the "
        f"caps frozen in EDGE.md 41d")


# ---------------------------------------------------------------------------
# monitors — pure functions of closed trades
# ---------------------------------------------------------------------------


def rolling_mean_r(trades: Sequence[ShadowTrade], k: int) -> Optional[float]:
    """Mean net R over the last `k` closed trades, or None if there are fewer."""
    if len(trades) < k or k <= 0:
        return None
    window = list(trades)[-k:]
    return sum(t.net_r for t in window) / float(k)


def rolling_mean_r_by_days(trades: Sequence[ShadowTrade], window_days: int,
                           *, asof: dt.date) -> Tuple[Optional[float], int]:
    """Mean net R over trades that CLOSED within `window_days` of `asof`.

    Keyed on the exit date, not the entry: a trade is only evidence once it is
    resolved. `asof` is passed in rather than read from the clock so the
    function is pure and a test can pin the window.
    """
    cutoff = asof - dt.timedelta(days=window_days)
    inside = [t for t in trades if cutoff <= t.exit_date() <= asof]
    if not inside:
        return None, 0
    return sum(t.net_r for t in inside) / float(len(inside)), len(inside)


def concentration_top3(trades: Sequence[ShadowTrade],
                       top_n: int = 3) -> Optional[float]:
    """Share of the summed net R carried by the best `top_n` trades.

    Returns None when the sum is not positive: a "share of a non-positive
    total" is not a number anyone should act on, and reporting one would be
    worse than reporting nothing. The OOS sample's own value is 0.41.
    """
    if not trades:
        return None
    values = sorted((t.net_r for t in trades), reverse=True)
    total = sum(values)
    if total <= 0.0:
        return None
    return sum(values[:top_n]) / total


def halves_split(trades: Sequence[ShadowTrade]
                 ) -> Tuple[Optional[float], Optional[float], int, int]:
    """(earlier mean, recent mean, earlier n, recent n), split by position."""
    n = len(trades)
    if n < 2:
        return None, None, 0, 0
    cut = n // 2
    earlier, recent = list(trades)[:cut], list(trades)[cut:]
    return (sum(t.net_r for t in earlier) / len(earlier),
            sum(t.net_r for t in recent) / len(recent),
            len(earlier), len(recent))


def evaluate_monitors(trades: Sequence[ShadowTrade], *,
                      asof: Optional[dt.date] = None) -> List[MonitorReading]:
    """All four monitors, against the thresholds frozen in EDGE.md §41e.

    `asof` is a parameter, not `date.today()`, so this is a pure function: the
    same trades and the same date always produce the same readings, and a test
    does not have to mock a clock.
    """
    if asof is None:
        asof = (max(t.exit_date() for t in trades) if trades
                else dt.date(1970, 1, 1))
    out: List[MonitorReading] = []

    # M-1 rolling trades
    cfg = MONITOR_THRESHOLDS["M1_rolling_trades"]
    value = rolling_mean_r(trades, cfg["k"])
    if value is None:
        out.append(MonitorReading(
            "M1_rolling_trades", "INSUFFICIENT_DATA", None,
            cfg["alert_below"], len(trades),
            f"needs {cfg['k']} closed trades, has {len(trades)}"))
    else:
        state = ("ALERT" if value < cfg["alert_below"]
                 else "WARN" if value < cfg["warn_below"] else "OK")
        out.append(MonitorReading(
            "M1_rolling_trades", state, value, cfg["alert_below"], cfg["k"],
            f"mean net R over the last {cfg['k']} closed trades"))

    # M-2 rolling days
    cfg = MONITOR_THRESHOLDS["M2_rolling_days"]
    value, n_in = rolling_mean_r_by_days(trades, cfg["window_days"], asof=asof)
    if value is None or n_in < cfg["min_trades"]:
        out.append(MonitorReading(
            "M2_rolling_days", "INSUFFICIENT_DATA", value, cfg["alert_below"],
            n_in,
            f"needs {cfg['min_trades']} trades closed in the last "
            f"{cfg['window_days']} days, has {n_in}"))
    else:
        state = ("ALERT" if value < cfg["alert_below"]
                 else "WARN" if value < cfg["warn_below"] else "OK")
        out.append(MonitorReading(
            "M2_rolling_days", state, value, cfg["alert_below"], n_in,
            f"mean net R over trades closed in the last {cfg['window_days']} "
            f"days"))

    # M-3 concentration
    cfg = MONITOR_THRESHOLDS["M3_concentration"]
    if len(trades) < cfg["min_trades"]:
        out.append(MonitorReading(
            "M3_concentration", "INSUFFICIENT_DATA", None, cfg["warn_above"],
            len(trades),
            f"needs {cfg['min_trades']} closed trades, has {len(trades)}"))
    else:
        value = concentration_top3(trades, cfg["top_n"])
        if value is None:
            out.append(MonitorReading(
                "M3_concentration", "INSUFFICIENT_DATA", None,
                cfg["warn_above"], len(trades),
                "cumulative net R is not positive; a share of it would not be "
                "a number worth acting on"))
        else:
            state = "WARN" if value > cfg["warn_above"] else "OK"
            out.append(MonitorReading(
                "M3_concentration", state, value, cfg["warn_above"],
                len(trades),
                f"top-{cfg['top_n']} share of cumulative net R "
                f"(the OOS sample itself is 0.41)"))

    # M-4 halves
    cfg = MONITOR_THRESHOLDS["M4_halves"]
    if len(trades) < cfg["min_trades"]:
        out.append(MonitorReading(
            "M4_halves", "INSUFFICIENT_DATA", None, None, len(trades),
            f"needs {cfg['min_trades']} closed trades, has {len(trades)}"))
    else:
        earlier, recent, n_e, n_r = halves_split(trades)
        state = "WARN" if (recent is not None and earlier is not None
                           and recent < 0.0 <= earlier) else "OK"
        out.append(MonitorReading(
            "M4_halves", state, recent, 0.0, len(trades),
            f"earlier half {earlier:+.4f} on {n_e}, recent half "
            f"{recent:+.4f} on {n_r}"))

    return out


def monitor_status(readings: Sequence[MonitorReading]) -> str:
    """The worst state present. ALERT > WARN > OK > INSUFFICIENT_DATA."""
    order = {"ALERT": 3, "WARN": 2, "OK": 1, "INSUFFICIENT_DATA": 0}
    if not readings:
        return "INSUFFICIENT_DATA"
    return max(readings, key=lambda r: order.get(r.state, 0)).state


def entries_blocked_by_monitor(readings: Sequence[MonitorReading]) -> bool:
    """Does the monitor set block new shadow entries?

    ALERT only. A WARN is information; blocking on it would make a monitor a
    trading rule, and §41e is explicit that monitors raise status and do not
    trade. Note what this still does NOT do even at ALERT: it does not resize,
    reverse, retune a constant, clear the kill switch, or touch the research
    flag.
    """
    return any(r.state == "ALERT" for r in readings)


# ---------------------------------------------------------------------------
# revocation — human only
# ---------------------------------------------------------------------------


def read_revocations(path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every revocation record on disk. Missing or unreadable file -> none.

    Deliberately fails OPEN here and closed at the call site: an unreadable
    revocations file must not silently un-revoke, so `is_revoked` treats a
    parse failure as "cannot confirm" and the registration hook, which is the
    thing that matters, is the one that refuses.
    """
    # Resolved at CALL time, not bound at def time, so a test (or an operator
    # pointing at a different tree) can redirect it. A default frozen into the
    # signature is a default that cannot be redirected, and the first test to
    # try discovered exactly that.
    path = path or REVOCATIONS_PATH
    if not path or not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception as exc:  # noqa: BLE001
        logger.error("revocations file %s is unreadable (%s); treating every "
                     "signal named in it as REVOKED", path, exc)
        return [{"signal": "*", "unreadable": True}]
    records = payload.get("revocations") if isinstance(payload, dict) else payload
    return list(records) if isinstance(records, list) else []


def is_revoked(signal: str, *, path: Optional[str] = None) -> bool:
    """Has a human revoked this signal's clear?

    A record only counts if it carries the exact acknowledgement. A file
    someone edited without knowing the token does not revoke anything, and —
    more importantly — a file that is corrupt revokes EVERYTHING, because
    "cannot tell" and "not revoked" must not be the same answer.
    """
    for record in read_revocations(path):
        if record.get("unreadable"):
            return True
        if record.get("signal") != signal:
            continue
        if str(record.get("acknowledgement")) == REVOCATION_ACK:
            return True
        logger.warning(
            "revocation record for %r does not carry the required "
            "acknowledgement; ignoring it. A human must write %r.",
            signal, REVOCATION_ACK)
    return False


def revoke_cleared_edge(signal: str, *, acknowledgement: str, reason: str,
                        operator: str, at_utc: str,
                        path: Optional[str] = None) -> Dict[str, Any]:
    """Record a HUMAN revocation. The only way a cleared edge is withdrawn.

    Requires the literal :data:`REVOCATION_ACK`, which is not derivable from
    config — the same asymmetry the kill switch uses, and for the same reason:
    a token that could be assembled from settings is a token a process can
    supply to itself.

    `at_utc` is a parameter rather than a clock read, so the record is
    reproducible and a test does not have to freeze time.

    **Nothing in the trading path, and nothing in any model module, may call
    this.** An AST test in `tests/test_shadow_pack.py` walks them and asserts
    so. A model that could revoke an edge is a model with an opinion about
    research.
    """
    if acknowledgement != REVOCATION_ACK:
        raise PermissionError(
            f"revoking a cleared edge requires the exact acknowledgement "
            f"{REVOCATION_ACK!r}. This is deliberately not derivable from "
            f"configuration: a token a process could assemble for itself is "
            f"not a human decision.")
    if not str(reason).strip():
        raise ValueError(
            "a revocation must carry a reason. A research claim withdrawn "
            "without one is not auditable.")
    if not str(operator).strip():
        raise ValueError("a revocation must name the human who made it.")

    record = {
        "signal": signal,
        "acknowledgement": acknowledgement,
        "reason": str(reason).strip(),
        "operator": str(operator).strip(),
        "revoked_at_utc": at_utc,
    }
    path = path or REVOCATIONS_PATH
    existing = read_revocations(path)
    existing = [r for r in existing if not r.get("unreadable")]
    existing.append(record)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"schema": "edge_revocations/1",
                   "revocations": existing}, handle, indent=2,
                  ensure_ascii=False)
        handle.write("\n")
    logger.error("CLEARED EDGE REVOKED BY %s: %s (%s)", operator, signal,
                 reason)
    return record
