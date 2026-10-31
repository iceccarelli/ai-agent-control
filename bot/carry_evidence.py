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

import time
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

import strategy_evidence as _se

NET_BASIS = ("ledger net USD per closed cycle after booked fees, funding and "
             "financing; ACTUAL_RECONCILED cycles only (tools/carry_performance)")


def evidence_from_report(report: Mapping[str, Any], *, strategy_id: str,
                         observed_at_s: float,
                         discovery_window_s: Optional[Tuple[float, float]],
                         drawdown_limit_usd: Optional[float]
                         ) -> _se.StrategyEvidence:
    """A `StrategyEvidence` from a performance report. Only the cycles the
    report itself lists as realized cash become trades."""
    sections = report.get("sections") or {}
    cash = sections.get("realized_cash") or {}
    ids = set(cash.get("realized_cash_cycle_ids") or ())
    trades = []
    if ids and cash.get("evidence_class") == "ACTUAL_RECONCILED":
        detail = (sections.get("ledger_detail") or {}).get("cycles") or []
        for c in detail:
            if c.get("cycle_id") not in ids:
                continue
            start, end, net = c.get("start_ms"), c.get("last_ms"), c.get("net_usd")
            if None in (start, end, net):
                continue                    # a cycle the report cannot time is not used
            trades.append(_se.EvidenceTrade(
                entry_s=float(start) / 1000.0, exit_s=float(end) / 1000.0,
                net=float(net)))
    return _se.StrategyEvidence(
        strategy_id=strategy_id, trades=tuple(trades),
        observed_at_s=float(observed_at_s), net_basis=NET_BASIS,
        discovery_window_s=discovery_window_s, drawdown_limit=drawdown_limit_usd)


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
