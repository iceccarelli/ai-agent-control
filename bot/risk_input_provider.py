"""The authoritative risk-input provider: builds a ``risk_policy.State`` from sources that can be MEASURED, and says UNKNOWN (None) for everything else.

Rules this module holds to, each pinned by tests/test_risk_input_provider.py:

  * A value it cannot establish is ``None``. Never a default, never 0, never an estimate. ``risk_policy.evaluate`` refuses a ``None`` (``MISSING_INPUT:<field>``), so an
    unmeasured field blocks entry; that is the intended behaviour, not something to paper over.
  * A source older than its stated limit makes ITS fields None. A venue read that raises makes its fields None and is recorded in ``unknown_reasons`` (field -> why).
  * ``state()`` never raises.
  * Quantities (perp and spot) come from the VENUE (``get_perp_net_qty``, ``get_spot_inventory``), never from the journal. The ledger supplies only what only it knows
    (open time, realised-loss figures), through callables the owner of the ledger supplies.
  * margin_multiple and liquidation_distance_pct are the broker's own functions (``get_margin_multiple`` / ``get_liquidation_view``), the same ones CarryEngine uses; this
    module does not re-derive them. Before a position exists the venue has no liquidation price and no margin ratio, so BOTH ARE UNKNOWN while flat: an entry-time policy
    that requires them refuses until the owner decides how a pre-open measurement should work. This module will not invent one.
  * correlated_exposure_usd is UNKNOWN unless a declared correlation set is supplied; then it is the labelled sum over that set.

Reads only. No order endpoint is touched.
"""
from __future__ import annotations

import math
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

import risk_policy as RP

#: Hard staleness limits of this module's own sources (seconds). A source older than its limit is UNKNOWN, whatever the policy's own max_data_age_s says.
MAX_SNAPSHOT_AGE_S = 30.0
MAX_BOOK_AGE_S = 10.0
MAX_FUNDING_AGE_S = 12 * 3600.0
#: A snapshot stamped further in the future than this is a broken clock, not a fresh one.
MAX_FUTURE_SKEW_S = 5.0

#: Every State field, in policy order (operational_fault last).
FIELDS: Tuple[str, ...] = (
    "trade_loss_usd", "loss_today_usd", "strategy_drawdown_usd", "portfolio_drawdown_usd", "hold_hours", "exposure_usd", "leverage", "liquidity_usd",
    "unhedged_usd", "margin_multiple", "liquidation_distance_pct", "data_age_s", "correlated_exposure_usd", "recent_mean_funding_bps", "operational_fault")
_LEDGER_FIELDS = ("trade_loss_usd", "loss_today_usd", "strategy_drawdown_usd", "portfolio_drawdown_usd")


def _num(x: Any, *, minimum: Optional[float] = None, strict: bool = False) -> Optional[float]:
    """A finite real number (not a bool), optionally bounded below; else None."""
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
        return None
    if minimum is not None and (x <= minimum if strict else x < minimum):
        return None
    return float(x)


class _Unknown(Exception):
    """Internal: the value cannot be established; ``args[0]`` is why."""


def _default_equity_reader(broker: Any) -> float:
    """``totalEquity`` of the UNIFIED account, STATED by the venue (reads only). Raises when absent, blank, non-finite or not positive."""
    result = broker.client._request("GET", "/v5/account/wallet-balance", signed=True, params={"accountType": "UNIFIED"})
    rows = broker._v5_list(result)
    if not rows or not isinstance(rows[0], dict):
        raise _Unknown("wallet balance unreadable")
    raw = rows[0].get("totalEquity")
    if raw is None or isinstance(raw, bool) or str(raw).strip() == "":
        raise _Unknown("wallet row does not state totalEquity")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise _Unknown(f"totalEquity {raw!r} is not a number")
    if not math.isfinite(value) or value <= 0:
        raise _Unknown(f"totalEquity {raw!r} is not a positive finite number")
    return value


class RiskInputProvider:
    """``state()`` -> ``risk_policy.State``; ``explain()`` -> per field {value, source, age_s, why_unknown}.

    broker            the CarryBroker (its existing venue reads are used).
    perp_symbol/spot_symbol
    snapshot_getter   () -> MarketSnapshot-like (perp_mark, observed_at_s) or None. The mark and ``data_age_s`` come from its own timestamps.
    opened_ms_getter  () -> the open time (epoch ms) of the held position from the venue/ledger, or None.
    halted_getter     () -> True/False if the book states HALTED, or None when it does not know.
    ledger_reads      {field: () -> Optional[float]} for trade_loss_usd / loss_today_usd / strategy_drawdown_usd / portfolio_drawdown_usd. A field with no entry is UNKNOWN.
    funding_getter    () -> (mean_funding_bps, as_of_ms) or None.
    correlation_set   {label: () -> Optional[float exposure_usd]}: the DECLARED set. None/empty means correlated exposure is UNKNOWN.
    equity_reader     () -> equity USD (default: the venue wallet's totalEquity). Must raise or return a positive finite number.
    now_s             clock (default time.time).
    """

    def __init__(self, *, broker: Any, perp_symbol: str, spot_symbol: str,
                 snapshot_getter: Callable[[], Any],
                 opened_ms_getter: Optional[Callable[[], Optional[int]]] = None,
                 halted_getter: Optional[Callable[[], Optional[bool]]] = None,
                 ledger_reads: Optional[Mapping[str, Callable[[], Optional[float]]]] = None,
                 funding_getter: Optional[Callable[[], Optional[Tuple[float, int]]]] = None,
                 correlation_set: Optional[Mapping[str, Callable[[], Optional[float]]]] = None,
                 equity_reader: Optional[Callable[[], float]] = None,
                 now_s: Callable[[], float] = time.time,
                 max_snapshot_age_s: float = MAX_SNAPSHOT_AGE_S,
                 max_book_age_s: float = MAX_BOOK_AGE_S,
                 max_funding_age_s: float = MAX_FUNDING_AGE_S) -> None:
        self.broker = broker
        self.perp_symbol, self.spot_symbol = perp_symbol, spot_symbol
        self.snapshot_getter = snapshot_getter
        self.opened_ms_getter, self.halted_getter = opened_ms_getter, halted_getter
        self.ledger_reads = dict(ledger_reads or {})
        self.funding_getter = funding_getter
        self.correlation_set = dict(correlation_set or {})
        self.equity_reader = equity_reader or (lambda: _default_equity_reader(broker))
        self.now_s = now_s
        self.max_snapshot_age_s, self.max_book_age_s, self.max_funding_age_s = max_snapshot_age_s, max_book_age_s, max_funding_age_s
        self.unknown_reasons: Dict[str, str] = {}
        self._last: Optional[Dict[str, Dict[str, Any]]] = None

    # -- public -----------------------------------------------------------------------------------------------------------------------------------------------

    def state(self) -> RP.State:
        """Never raises. Any unexpected failure leaves the whole state UNKNOWN and says why."""
        try:
            facts = self._measure()
        except Exception as exc:  # noqa: BLE001 - the contract is: never raise out of state()
            facts = {f: self._fact(None, "provider", None, f"PROVIDER_ERROR:{type(exc).__name__}") for f in FIELDS}
        self._last = facts
        self.unknown_reasons = {f: d["why_unknown"] for f, d in facts.items() if d["value"] is None}
        return RP.State(**{f: facts[f]["value"] for f in FIELDS})

    def explain(self, refresh: bool = False) -> Dict[str, Dict[str, Any]]:
        """Per State field ``{value, source, age_s, why_unknown}``; the last ``state()`` measurement unless ``refresh`` (or none yet)."""
        if refresh or self._last is None:
            self.state()
        assert self._last is not None
        return {f: dict(d) for f, d in self._last.items()}

    # -- measurement ------------------------------------------------------------------------------------------------------------------------------------------

    @staticmethod
    def _fact(value: Any, source: str, age_s: Optional[float], why: str = "") -> Dict[str, Any]:
        return {"value": value, "source": source, "age_s": age_s, "why_unknown": "" if value is not None else (why or "UNKNOWN")}

    def _read(self, label: str, fn: Callable[[], Any]) -> Tuple[Any, str]:
        """(value, '') or (None, why). A raising read is UNKNOWN and says what raised."""
        try:
            return fn(), ""
        except _Unknown as exc:
            return None, f"{label}: {exc}"
        except Exception as exc:  # noqa: BLE001
            return None, f"{label} read raised {type(exc).__name__}: {exc}"

    def _measure(self) -> Dict[str, Dict[str, Any]]:
        now = float(self.now_s())
        out: Dict[str, Dict[str, Any]] = {}
        F = self._fact

        # ---- snapshot: mark and data age, from the snapshot's OWN timestamps
        mark: Optional[float] = None
        age: Optional[float] = None
        snap_why = ""
        snap, err = self._read("snapshot", self.snapshot_getter)
        if snap is None:
            snap_why = err or "snapshot: no market snapshot"
        else:
            observed = _num(getattr(snap, "observed_at_s", None), minimum=0.0)
            raw_mark = _num(getattr(snap, "perp_mark", None), minimum=0.0, strict=True)
            if observed is None:
                snap_why = "snapshot: observed_at_s missing or not a time"
            elif observed - now > MAX_FUTURE_SKEW_S:
                snap_why = f"snapshot: observed_at_s is {observed - now:.1f}s in the future (broken clock)"
            else:
                age = max(0.0, now - observed)
                if raw_mark is None:
                    snap_why = "snapshot: perp_mark missing or not a positive finite number"
                elif age > self.max_snapshot_age_s:
                    snap_why = f"snapshot: {age:.1f}s old, limit {self.max_snapshot_age_s:.0f}s (stale)"
                else:
                    mark = raw_mark
        out["data_age_s"] = F(age, "market_snapshot.observed_at_s", age, snap_why or "snapshot unavailable")

        # ---- venue quantities (never the journal)
        perp_net, why_perp = self._read("perp position", lambda: self._finite_or_unknown(self.broker.get_perp_net_qty(self.perp_symbol), "perp_net_qty"))
        spot_qty, why_spot = self._read("spot inventory", lambda: self._finite_or_unknown(self.broker.get_spot_inventory(self.spot_symbol), "spot_inventory"))
        if perp_net is not None:
            perp_net = float(perp_net)
        if spot_qty is not None:
            spot_qty = float(spot_qty)

        if perp_net is not None and mark is not None:
            out["exposure_usd"] = F(abs(perp_net) * mark, "venue perp qty x snapshot perp_mark", age)
        else:
            out["exposure_usd"] = F(None, "venue perp qty x snapshot perp_mark", None, why_perp or snap_why or "UNKNOWN")
        if perp_net is not None and spot_qty is not None and mark is not None:
            out["unhedged_usd"] = F(abs(perp_net + spot_qty) * mark, "|venue perp signed + venue spot| x snapshot perp_mark", age)
        else:
            out["unhedged_usd"] = F(None, "|venue perp signed + venue spot| x snapshot perp_mark", None, why_perp or why_spot or snap_why or "UNKNOWN")

        # ---- leverage = exposure / equity
        equity, why_eq = self._read("equity", self.equity_reader)
        equity = _num(equity, minimum=0.0, strict=True)
        if equity is None and not why_eq:
            why_eq = "equity: not a positive finite number"
        exp = out["exposure_usd"]["value"]
        if exp is not None and equity is not None:
            out["leverage"] = F(exp / equity, "exposure_usd / venue totalEquity", age)
        else:
            out["leverage"] = F(None, "exposure_usd / venue totalEquity", None, why_eq or out["exposure_usd"]["why_unknown"])

        # ---- hold hours: flat on the venue is 0 held; otherwise the open time must be stated
        if perp_net is None:
            out["hold_hours"] = F(None, "venue flat / ledger opened_ms", None, why_perp)
        elif perp_net == 0.0:
            out["hold_hours"] = F(0.0, "venue position read says flat", 0.0)
        else:
            opened, why_open = self._read("opened_ms", self.opened_ms_getter) if self.opened_ms_getter else (None, "no opened_ms source wired")
            opened_n = _num(opened, minimum=0.0, strict=True)
            if opened_n is None:
                out["hold_hours"] = F(None, "ledger opened_ms", None, why_open or "opened_ms unknown (0/absent is not a time)")
            elif opened_n / 1000.0 - now > MAX_FUTURE_SKEW_S:
                out["hold_hours"] = F(None, "ledger opened_ms", None, "opened_ms is in the future")
            else:
                out["hold_hours"] = F(max(0.0, now - opened_n / 1000.0) / 3600.0, "ledger opened_ms", None)

        # ---- margin and liquidation: the broker's own functions, only on a held position
        if perp_net is None:
            why_m = why_perp
            out["margin_multiple"] = F(None, "broker.get_margin_multiple", None, why_m)
            out["liquidation_distance_pct"] = F(None, "broker.get_liquidation_view", None, why_m)
        elif perp_net == 0.0:
            flat = "FLAT: the venue has no position, so no margin ratio and no liquidation price exist before opening"
            out["margin_multiple"] = F(None, "broker.get_margin_multiple", None, flat)
            out["liquidation_distance_pct"] = F(None, "broker.get_liquidation_view", None, flat)
        else:
            mm, why_mm = self._read("margin_multiple", lambda: self._finite_or_unknown(self.broker.get_margin_multiple(self.perp_symbol), "margin_multiple", strict=True))
            out["margin_multiple"] = F(float(mm) if mm is not None else None, "broker.get_margin_multiple (venue positionIM/positionMM)", 0.0, why_mm)
            view, why_view = self._read("liquidation_view", lambda: self.broker.get_liquidation_view(self.perp_symbol))
            dist, why_d = None, why_view
            if isinstance(view, Mapping):
                vsize = _num(view.get("size"), minimum=0.0)
                if vsize is None or not math.isclose(vsize, abs(perp_net), rel_tol=1e-6, abs_tol=1e-9):
                    why_d = f"liquidation_view size {view.get('size')!r} disagrees with venue position {abs(perp_net)!r}"
                elif view.get("distance_pct") is None:
                    why_d = f"liquidation_view carries no distance (reason {view.get('reason')!r})"
                else:
                    dist = _num(view.get("distance_pct"), minimum=0.0)
                    if dist is None:
                        why_d = f"liquidation distance {view.get('distance_pct')!r} is not a non-negative finite number"
            elif view is not None:
                why_d = f"liquidation_view is {type(view).__name__}, not a mapping"
            out["liquidation_distance_pct"] = F(dist, "broker.get_liquidation_view (venue liqPrice vs markPrice)", 0.0, why_d)

        # ---- liquidity: the thinner side of the perp book ladder just read
        out["liquidity_usd"] = self._liquidity(now)

        # ---- ledger-sourced figures
        for name in _LEDGER_FIELDS:
            fn = self.ledger_reads.get(name)
            if fn is None:
                out[name] = F(None, "ledger", None, f"no ledger source wired for {name}")
                continue
            v, why = self._read(name, fn)
            n = _num(v, minimum=0.0)
            out[name] = F(n, "ledger", None, why or f"{name} unstated or not a non-negative finite number")

        # ---- funding
        out["recent_mean_funding_bps"] = self._funding(now)

        # ---- correlated exposure: only over a DECLARED set
        out["correlated_exposure_usd"] = self._correlated()

        # ---- operational fault: halted flag AND a readable open-orders list
        out["operational_fault"] = self._fault()
        return {f: out[f] for f in FIELDS}

    @staticmethod
    def _finite_or_unknown(x: Any, what: str, *, strict: bool = False) -> float:
        v = _num(x)
        if v is None or (strict and v <= 0):
            raise _Unknown(f"{what} is {x!r}")
        return v

    def _liquidity(self, now: float) -> Dict[str, Any]:
        src = f"min(bid, ask) ladder notional of {self.perp_symbol} book (public orderbook)"
        book, why = self._read("orderbook", lambda: self.broker.get_book_depth(self.perp_symbol, "linear"))
        if not isinstance(book, Mapping):
            return self._fact(None, src, None, why or "orderbook unavailable")
        ts = _num(book.get("venue_ts_ms"), minimum=0.0, strict=True)
        age = 0.0
        if ts is not None:
            age = max(0.0, now - ts / 1000.0)
            if age > self.max_book_age_s:
                return self._fact(None, src, age, f"orderbook: {age:.1f}s old, limit {self.max_book_age_s:.0f}s (stale)")
        try:
            sides = []
            for key in ("bids", "asks"):
                total = 0.0
                for price, size in book[key]:
                    p, s = _num(price, minimum=0.0, strict=True), _num(size, minimum=0.0)
                    if p is None or s is None:
                        raise ValueError("bad level")
                    total += p * s
                sides.append(total)
        except Exception:  # noqa: BLE001
            return self._fact(None, src, age, "orderbook ladder unusable")
        if not sides or min(sides) <= 0:
            return self._fact(None, src, age, "orderbook has an empty side")
        return self._fact(min(sides), src, age)

    def _funding(self, now: float) -> Dict[str, Any]:
        src = "funding history (as supplied)"
        if self.funding_getter is None:
            return self._fact(None, src, None, "no funding source wired")
        got, why = self._read("funding", self.funding_getter)
        if got is None:
            return self._fact(None, src, None, why or "funding source returned nothing")
        try:
            mean, as_of_ms = got
        except (TypeError, ValueError):
            return self._fact(None, src, None, "funding source did not return (mean_bps, as_of_ms)")
        mean_n, as_of = _num(mean), _num(as_of_ms, minimum=0.0, strict=True)
        if mean_n is None or as_of is None:
            return self._fact(None, src, None, "funding mean or as-of time is not a finite number")
        age = now - as_of / 1000.0
        if age < -MAX_FUTURE_SKEW_S:
            return self._fact(None, src, None, "funding as-of time is in the future")
        age = max(0.0, age)
        if age > self.max_funding_age_s:
            return self._fact(None, src, age, f"funding: {age:.0f}s old, limit {self.max_funding_age_s:.0f}s (stale)")
        return self._fact(mean_n, src, age)

    def _correlated(self) -> Dict[str, Any]:
        if not self.correlation_set:
            return self._fact(None, "declared correlation set", None, "no declared correlation set: correlation safety is not measured")
        total, labels = 0.0, sorted(self.correlation_set)
        for label in labels:
            v, why = self._read(label, self.correlation_set[label])
            n = _num(v, minimum=0.0)
            if n is None:
                return self._fact(None, f"declared correlation set {labels}", None, why or f"member {label!r} exposure unstated")
            total += n
        return self._fact(total, f"sum over declared correlation set {labels}", None)

    def _fault(self) -> Dict[str, Any]:
        src = "carry state HALTED flag + readable open-orders list"
        if self.halted_getter is None:
            return self._fact(None, src, None, "no halted-state source wired")
        halted, why = self._read("halted_state", self.halted_getter)
        if halted is None:
            return self._fact(None, src, None, why or "the book did not state whether it is halted")
        if not isinstance(halted, bool):
            return self._fact(None, src, None, f"halted flag {halted!r} is not a boolean")
        if halted:
            return self._fact(True, src, None)
        orders, why_o = self._read("open orders", lambda: self.broker.get_open_carry_orders(self.spot_symbol, self.perp_symbol))
        if orders is None:
            return self._fact(None, src, None, why_o or "open orders unreadable")      # cannot say there is no fault
        return self._fact(False, src, 0.0)
