"""The carry book's strategy-evidence verdict, from its own reconciled results.

`strategy_evidence.evaluate_strategy_evidence` judges evidence; it does not
find any. This module supplies the carry book's: the closed cycles of its own
ledger that the performance report classifies ACTUAL_RECONCILED (real money,
fees and funding stated by the venue, every order and funding row reconciled).
Nothing else counts: testnet, paper, simulated, forward-shadow and
mark-to-market figures are not cash and are not trades here.

Two inputs are the OWNER's and are never defaulted: the discovery window (the
period in which the strategy was designed and tuned, so its evidence must come
from after it) and the drawdown limit. Unset, they stay None and the verdict is
INSUFFICIENT; this module will not choose either.

The verdict is an input to the entry gate's evidence layer only. It authorises
nothing by itself (`grants_owner_approval` is always False).
"""
from __future__ import annotations

import math
import time
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

import strategy_evidence as _se

#: Cycle outcomes of `carry_performance` that are results (the other is OPEN).
_RESULT_OUTCOMES = ("CLOSED", "ABORTED_BEFORE_HEDGE")

NET_BASIS = ("ledger net USD per closed cycle after booked fees, funding and "
             "financing; ACTUAL_RECONCILED cycles only (tools/carry_performance); "
             "overlay cycles are never counted: their cash is the short's "
             "result alone and their pair economics rest on a marked, unsold "
             "inventory leg, which is not cash")


def _num(x: Any) -> Optional[float]:
    """A finite float from a real number only (bool and text are not numbers)."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    v = float(x)
    return v if math.isfinite(v) else None


def _is_corroborated_mainnet(report: Mapping[str, Any]) -> bool:
    """Does the report's OWN environment block say corroborated mainnet? A
    section's label is not trusted by itself: it must agree with the block the
    report derived from the database, and the report must not admit a broken
    ledger. Anything missing or different is no."""
    env = report.get("environment")
    if not isinstance(env, Mapping):
        return False
    return (env.get("declared") == "mainnet"
            and env.get("actual_reconciled_eligible") is True
            and env.get("recorded_seen") == ["mainnet"]
            # what classify_environment assigns a corroborated mainnet ledger's
            # non-cash figures; any other class contradicts the eligibility flag
            and env.get("booked_figures_class") == "UNAVAILABLE"
            and report.get("invariants_hold") is True
            and report.get("unresolved_carry_orders") == 0)


def _cycle_is_cash(c: Mapping[str, Any]) -> bool:
    """A cycle as the report's detail shows it: closed, reconciled cash by its
    own account, fees and funding complete."""
    return (isinstance(c, Mapping)
            and c.get("evidence_class") == "ACTUAL_RECONCILED"
            and c.get("closed") is True
            and c.get("outcome") in _RESULT_OUTCOMES
            and c.get("fees_complete") is True
            and c.get("funding_complete") is True)


def evidence_from_report(report: Mapping[str, Any], *, strategy_id: str,
                         observed_at_s: float,
                         discovery_window_s: Optional[Tuple[float, float]],
                         drawdown_limit_usd: Optional[float]
                         ) -> _se.StrategyEvidence:
    """A `StrategyEvidence` from a performance report. Only the cycles the
    report itself lists as realized cash, AND whose own record says reconciled
    closed cash, AND only from a report whose environment block is corroborated
    mainnet, become trades. Closed results the report excluded (and any it
    cannot account for) are counted in ``unqualified_results`` so the sample
    cannot silently omit its own losers."""
    sections = report.get("sections") if isinstance(report, Mapping) else None
    sections = sections if isinstance(sections, Mapping) else {}
    cash = sections.get("realized_cash")
    cash = cash if isinstance(cash, Mapping) else {}
    raw_ids = cash.get("realized_cash_cycle_ids")
    ids = [i for i in raw_ids if isinstance(i, str)] \
        if isinstance(raw_ids, (list, tuple)) else []
    trades = []
    taken: set = set()
    mainnet_cash = (_is_corroborated_mainnet(report)
                    and cash.get("evidence_class") == "ACTUAL_RECONCILED")
    unqualified = 0
    if mainnet_cash and ids:
        ledger = sections.get("ledger_detail")
        detail = ledger.get("cycles") if isinstance(ledger, Mapping) else None
        want = set(ids)
        for c in (detail if isinstance(detail, (list, tuple)) else ()):
            cid = c.get("cycle_id") if isinstance(c, Mapping) else None
            if cid not in want:
                continue                    # not listed as cash
            if cid in taken:
                unqualified += 1            # the same cycle twice: inconsistent
                continue
            start, end, net = _num(c.get("start_ms")), _num(c.get("end_ms")), \
                _num(c.get("net_usd"))
            if (None in (start, end, net) or end < start or not _cycle_is_cash(c)):
                continue                    # a cycle the report cannot vouch for
            # An overlay hedges BTC the client already owns and the book never
            # sells. Its cash is the short's result alone (the hedge is not in
            # it) and its pair economics rest on a MARKED, unsold inventory
            # leg: neither is cash evidence of a hedged carry, and a cash-only
            # policy must not be satisfied by marks. Such a cycle is therefore
            # never a trade here; it is counted in `unqualified_results`, which
            # keeps the verdict INSUFFICIENT rather than silently dropping it.
            # Only a cycle that is explicitly NOT an overlay (spot really
            # bought and sold: both legs are cash) can count, and a cycle that
            # does not say is refused.
            if c.get("overlay") is not False:
                continue
            taken.add(cid)
            trades.append(_se.EvidenceTrade(
                entry_s=start / 1000.0, exit_s=end / 1000.0, net=net))
        # a listed cash id with no usable cycle behind it is a missing result,
        # a repeated id or a count that disagrees with the list is inconsistent
        unqualified += len(want - taken) + (len(ids) - len(want))
        if cash.get("realized_cash_cycles") != len(ids):
            unqualified += 1
    # closed cycles the report booked but did NOT call cash
    booked = sections.get("closed_cycles_booked")
    count = booked.get("cycle_count") if isinstance(booked, Mapping) else None
    if mainnet_cash:
        unqualified += (count if isinstance(count, int)
                        and not isinstance(count, bool) and count >= 0 else 1)
    return _se.StrategyEvidence(
        strategy_id=strategy_id, trades=tuple(trades),
        observed_at_s=float(observed_at_s), net_basis=NET_BASIS,
        discovery_window_s=discovery_window_s, drawdown_limit=drawdown_limit_usd,
        evidence_class=(_se.CASH_EVIDENCE_CLASS if mainnet_cash else
                        "UNAVAILABLE"),
        unqualified_results=unqualified)


def _default_report() -> Optional[Callable[..., Dict[str, Any]]]:
    """`tools/carry_performance.build_report`, imported the way the tool itself
    is run (`tools/` is a directory of scripts, not a package). None when it is
    not shipped: then there is no verdict, which is not a pass."""
    import os
    import sys
    tools = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    try:
        import carry_performance                       # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    return carry_performance.build_report


def make_provider(*, db_path: str, environment: str, strategy_id: str,
                  discovery_window_s: Any,
                  drawdown_limit_usd: Any,
                  report_fn: Optional[Callable[..., Dict[str, Any]]] = None,
                  now_fn: Callable[[], float] = time.time
                  ) -> Callable[[], Optional[_se.EvidenceVerdict]]:
    """A zero-argument provider for the entry gate. It re-evaluates on every
    call (no cached verdict), reads the ledger read-only, and returns None when
    the report cannot be built: no verdict is not a pass.

    `discovery_window_s` and `drawdown_limit_usd` may each be a value or a
    zero-argument callable, which is called at every verdict."""
    def resolve(x: Any) -> Any:
        return x() if callable(x) else x

    def provider() -> Optional[_se.EvidenceVerdict]:
        build = report_fn
        if build is None:
            build = _default_report()
            if build is None:
                return None
        now = float(now_fn())
        try:
            report = build(db_path, environment=environment,
                           now_ms=int(now * 1000))
        except Exception:  # noqa: BLE001 - an unreadable ledger is no verdict
            return None
        evidence = evidence_from_report(
            report, strategy_id=strategy_id, observed_at_s=now,
            discovery_window_s=resolve(discovery_window_s),
            drawdown_limit_usd=resolve(drawdown_limit_usd))
        return _se.evaluate_strategy_evidence(evidence, now_s=now)
    return provider
