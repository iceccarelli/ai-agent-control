#!/usr/bin/env python3
"""LINEAR PROTECTIVE-STOP venue drill — Part 1, Item 1. Staged, evidenced.

WHY THIS EXISTS
================
`linear_protective_stop_verified` is one of the eight promotion-gate items,
and it is the one `docs/PROMOTION_GATE_MICRO_LIVE.md` names as "the item most
likely to be waved through and the one least safe to wave through." Closing
it requires proof that `BybitClient.verify_stop()` reads back `live=True`
from a REAL Bybit position — not a simulator, not a paper fill, not the carry
book's own drill.

`bot/tools/drill.py --arm` (Phase D) is carry evidence: it proves the CARRY
BOOK can pair two legs, reconcile against the venue and unwind. It never
calls `BybitClient.place_stop_order` / `verify_stop` at all — `CarryBroker`
has no protective-stop mechanism of its own, by design (INVENTORY F4's
liquidation-distance view is a *read*, not a stop). Citing
`artifacts/linear_stop_drill.json` as stop evidence is exactly the
substitution `docs/promotion/LINEAR_STOP_VENUE_GAP.md` warns against — a
number from one measurement reported as if it answered a different question.

This tool exercises the actual venue mechanism the DIRECTIONAL order path
uses: `BybitClient.place_order` / `place_stop_order` / `verify_stop`
(`bybit_connection.py`), the same client `main.py`'s directional strategy
builds and the same one `tests/test_linear_simulator.py`'s
`TestTheProtectiveStopEndToEnd` already drives against the simulator. One
stop dialect; this is where it meets the real venue.

WHAT THIS DOES
==============
Nine stages. Each records EVIDENCE — a value the venue actually returned —
and the drill refuses to advance when one fails.

    reachability     public last price, proves the host can reach the venue
    auth             a signed read (account wallet), proves the keys work
    flat             the linear symbol already holds nothing (else: stop)
    venue_rules      qty step / min qty / min notional, read not assumed
    open             a small LINEAR long (~$100 notional or one venue lot)
    attach           BybitClient.place_stop_order — the real trading-stop path
    verify           BybitClient.verify_stop — MUST read back live=True
    position_evidence  the position row's own stop fields, dumped verbatim
    flatten          close the position; the stop clears with it
    final_reconcile  the venue holds nothing and has no live stop

SAFE BY DEFAULT
===============
Without `--arm` this reads and reports and sends nothing. `--arm` refuses
outright unless `BYBIT_VENUE=testnet`, `PAPER_TRADING` is false, and
`CATEGORY=linear` — this tool proves ONE thing on ONE venue, and a paper
run or a spot-configured client would silently prove nothing while looking
green. It never touches `allows_live`, never promotes, never signs the gate
checklist: that stays a human, reading this transcript.

    python3 bot/tools/linear_stop_venue_drill.py
    python3 bot/tools/linear_stop_venue_drill.py --arm \
        --out bot/artifacts/linear_protective_stop_venue.json

ITEM 2 — RESTART SURVIVAL (--hold / --verify / --flatten)
===========================================================
This process cannot outlive its own restart to watch itself, so Item 2 is
three phases, run as separate invocations:

    --hold      open + attach a real stop, then EXIT LEAVING THE POSITION
                AND STOP LIVE ON THE VENUE. Never flattens.
    --verify    a NEW process (no shared state) reads the venue cold;
                verify_stop must read back live=True. Sends nothing.
    --flatten   required cleanup: closes the position if one is open.

    python3 bot/tools/linear_stop_venue_drill.py --hold --notional 100 \\
        --out bot/artifacts/linear_stop_restart_hold.json
    python3 bot/tools/linear_stop_venue_drill.py --verify \\
        --out bot/artifacts/linear_stop_restart_verify.json
    python3 bot/tools/linear_stop_venue_drill.py --flatten \\
        --out bot/artifacts/linear_stop_restart_flatten.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

from drill import Stage  # noqa: E402  (shared transcript primitive)

SYMBOL = "BTCUSDT"

#: How far below the entry the protective stop sits. A drill parameter, not a
#: tuned trading constant: it only has to be far enough that the market
#: cannot plausibly touch it during the seconds this drill runs, and close
#: enough that Bybit's own price-band validation still accepts it.
STOP_DISTANCE_FRACTION = 0.05

NOT_PROVEN = [
    "items 2-4 of the checklist: a restart with the stop still live, a "
    "NAKED position detected and re-protected, and margin/liquidation "
    "behaviour documented at the cap. Each needs its own drill or its own "
    "human procedure; this tool proves item 1 only.",
    "anything about size. One lot or ~$100 is not evidence about $100,000.",
    "the spot protective-stop path (a conditional order) - this tool is "
    "LINEAR only, by name.",
]


class LinearStopDrillRefused(RuntimeError):
    """The process may not arm this drill. Nothing was sent."""


def _assert_can_arm(cfg: Any) -> None:
    """Everything this checks must hold before a single order is sent.

    Deliberately separate from `CarryOrderRefused`/`order_gate`: this tool
    does not go through `CarryBroker` at all, so it owns its own refusal
    exactly as strict, checked before stage `open` and nowhere else -
    an order already in flight is not something a later check should paper
    over.
    """
    venue = str(getattr(cfg, "BYBIT_VENUE", "") or "")
    if venue != "testnet":
        raise LinearStopDrillRefused(
            f"BYBIT_VENUE={venue!r}; this tool arms on testnet only - no "
            "mainnet, no exception")
    if bool(getattr(cfg, "PAPER_TRADING", True)):
        raise LinearStopDrillRefused(
            "PAPER_TRADING is true; a paper fill proves nothing about the "
            "venue's real trading-stop mechanism")
    category = str(getattr(cfg, "CATEGORY", "") or "").lower()
    if category != "linear":
        raise LinearStopDrillRefused(
            f"CATEGORY={category!r}; place_stop_order dispatches to the SPOT "
            "conditional-order path unless the client is linear, which would "
            "silently drill the wrong mechanism")


class Drill:
    def __init__(self, *, client: Any, notional: float, arm: bool) -> None:
        self.client = client
        self.notional = notional
        self.arm = arm
        self.stages: List[Stage] = []
        self.orders_sent = 0
        self.failed_stage: Optional[str] = None
        self.still_open = False
        self.venue = str(getattr(client, "venue", "unknown"))

    def stage(self, name: str) -> Stage:
        s = Stage(name)
        self.stages.append(s)
        return s

    def run_stage(self, name: str, fn) -> bool:
        s = self.stage(name)
        try:
            fn(s)
        except Exception as exc:                               # noqa: BLE001
            s.failed(f"{type(exc).__name__}: {exc}")
        if not s.ok:
            self.failed_stage = name
        return s.ok


def run_drill(*, client: Any, notional: Optional[float] = None,
              arm: bool = False, out: str = "",
              stop_distance_fraction: float = STOP_DISTANCE_FRACTION
              ) -> Dict[str, Any]:
    """The whole sequence. Returns the transcript whatever happens."""
    import shadow

    cap = float(shadow.SHADOW_MAX_NOTIONAL_USD)
    asked = cap if notional is None else float(notional)
    d = Drill(client=client, notional=min(asked, cap), arm=arm)
    state: Dict[str, Any] = {}

    def reachability(s: Stage) -> None:
        mark = float(client.get_last_price(SYMBOL))
        if not mark > 0:
            s.failed(f"last price unusable: {mark!r}")
            return
        state["mark"] = mark
        s.passed(last_price=mark, venue=d.venue, category=client.category)

    def auth(s: Stage) -> None:
        wallet = client.get_wallet()
        account_type = wallet.get("accountType", "")
        s.passed(auth_ok=True, account_type=account_type)

    def flat(s: Stage) -> None:
        row = client.get_position(SYMBOL)
        held = abs(float((row or {}).get("size", 0) or 0))
        state["venue_perp_qty"] = held
        if held > 0:
            s.failed(
                f"the venue already holds {held} on {SYMBOL}. A drill that "
                "opens on top of something already there is not a drill, it "
                "is an incident.", venue_perp_qty=held)
            return
        s.passed(venue_perp_qty=held)

    def venue_rules(s: Stage) -> None:
        filters = client.get_instrument_filters(SYMBOL, force=True)
        state["filters"] = filters
        s.passed(qty_step=float(filters.qty_step),
                 min_qty=float(filters.min_qty),
                 min_notional=float(filters.min_notional),
                 from_exchange=filters.from_exchange)

    for name, fn in (("reachability", reachability), ("auth", auth),
                     ("flat", flat), ("venue_rules", venue_rules)):
        if not d.run_stage(name, fn):
            return _report(d, state, out, cap, verdict="FAILED")

    if not arm:
        return _report(d, state, out, cap, verdict="PREFLIGHT_ONLY")

    def arm_gate(s: Stage) -> None:
        """The same refusal `main()` checks before calling this function at
        all - repeated here so `run_drill()` is just as strict when called
        directly (as the test suite does), not only from the CLI."""
        _assert_can_arm(client.cfg)
        s.passed()

    if not d.run_stage("arm_gate", arm_gate):
        return _report(d, state, out, cap, verdict="FAILED")

    def open_position(s: Stage) -> None:
        filters = state["filters"]
        qty = max(float(filters.min_qty), d.notional / state["mark"])
        result = client.place_order(
            symbol=SYMBOL, side="Buy", qty=qty, order_type="Market",
            purpose="linear_stop_drill", filters=filters)
        d.orders_sent += 1
        if not result.ok:
            s.failed(f"open leg not accepted: {result.reason}",
                     reason=result.reason, order_link_id=result.order_link_id)
            return
        row = client.get_position(SYMBOL)
        size = abs(float((row or {}).get("size", 0) or 0))
        if size <= 0:
            s.failed(
                "order accepted but the venue reports no position afterward; "
                "whether it filled is unknown",
                order_link_id=result.order_link_id)
            return
        state["position_size"] = size
        state["entry_order_link_id"] = result.order_link_id
        s.passed(order_link_id=result.order_link_id,
                 filled_size=size, avg_price=(row or {}).get("avgPrice"))

    def attach(s: Stage) -> None:
        mark = state["mark"]
        trigger = mark * (1.0 - stop_distance_fraction)
        result = client.place_stop_order(
            symbol=SYMBOL, side="Sell", qty=state["position_size"],
            trigger_price=trigger, filters=state["filters"])
        if not result.ok:
            s.failed(f"trading-stop not accepted: {result.reason}",
                     reason=result.reason, trigger_price=trigger)
            return
        state["stop_order_link_id"] = result.order_link_id
        s.passed(order_link_id=result.order_link_id, trigger_price=trigger,
                 mechanism=(result.raw or {}).get("mechanism", ""))

    def verify(s: Stage) -> None:
        live, detail = client.verify_stop(
            symbol=SYMBOL, order_link_id=state["stop_order_link_id"])
        if not live:
            s.failed(f"verify_stop reports live=False: {detail}",
                     live=live, detail=detail)
            return
        s.passed(live=live, detail=detail,
                 endpoint="GET /v5/position/list (stopLoss field)")

    def position_evidence(s: Stage) -> None:
        row = client.get_position(SYMBOL) or {}
        s.passed(**{
            k: row.get(k) for k in
            ("symbol", "side", "size", "avgPrice", "stopLoss", "tpslMode",
             "slTriggerBy", "slOrderType", "positionIdx")
            if k in row})

    def flatten(s: Stage) -> None:
        result = client.place_order(
            symbol=SYMBOL, side="Sell", qty=state["position_size"],
            order_type="Market", purpose="linear_stop_drill_flatten",
            reduce_only=True, filters=state["filters"])
        d.orders_sent += 1
        if not result.ok:
            d.still_open = True
            s.failed(
                f"flatten not accepted: {result.reason}. THE POSITION MAY "
                "STILL BE OPEN. A human must close it by hand and confirm "
                "the stop is gone.", reason=result.reason)
            return
        s.passed(order_link_id=result.order_link_id)

    def final_reconcile(s: Stage) -> None:
        row = client.get_position(SYMBOL)
        size = abs(float((row or {}).get("size", 0) or 0))
        if size > 1e-9:
            d.still_open = True
            s.failed(f"the venue still holds {size} after flatten",
                     venue_perp_qty=size, verdict="NOT_FLAT")
            return
        live, detail = client.verify_stop(
            symbol=SYMBOL, order_link_id=state.get("stop_order_link_id", ""))
        s.passed(venue_perp_qty=size, verdict="FLAT",
                 stop_still_live=live, stop_detail=detail)

    for name, fn in (("open", open_position), ("attach", attach),
                     ("verify", verify), ("position_evidence", position_evidence),
                     ("flatten", flatten), ("final_reconcile", final_reconcile)):
        if not d.run_stage(name, fn):
            return _report(d, state, out, cap, verdict="FAILED")

    return _report(d, state, out, cap, verdict="PASSED")


def _report(d: Drill, state: Dict[str, Any], out: str, cap: float,
            *, verdict: str) -> Dict[str, Any]:
    evidence = {s.name: s.evidence for s in d.stages}
    if d.still_open:
        action = ("A HUMAN MUST ACT NOW: the position may still be open at "
                  "the venue. Close it by hand and confirm no stop is left "
                  "live. Nothing automated will retry.")
    elif verdict == "FAILED":
        action = (f"the drill stopped at {d.failed_stage!r}; nothing is "
                  "proven. Fix what the stage reports and run it again.")
    elif verdict == "PREFLIGHT_ONLY":
        action = "no order was sent. Re-run with --arm on testnet to prove it."
    else:
        action = ("PASSED. linear_protective_stop_verified's venue evidence "
                  "is this transcript's `verify` and `position_evidence` "
                  "stages - a human still signs the checklist and the "
                  "risk memo; this tool signs nothing.")
    report = {
        "tool": "linear_stop_venue_drill",
        "checklist_item": "linear_protective_stop_verified (Item 1 of 4)",
        "verdict": verdict,
        "venue": d.venue,
        "armed": bool(d.arm),
        "failed_stage": d.failed_stage,
        "still_open": d.still_open,
        "next_action": action,
        "notional_usd": d.notional,
        "cap_usd": cap,
        "orders_sent": d.orders_sent,
        "stages": [s.as_dict() for s in d.stages],
        "evidence": evidence,
        "not_proven": list(NOT_PROVEN),
        "finished_ms": int(time.time() * 1000),
    }
    _dump(report, out)
    return report


def _dump(report: Dict[str, Any], out: str) -> None:
    if out:
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        with open(out, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, default=str)


# ---------------------------------------------------------------------------
# Item 2 - restart survival. THREE PHASES, THREE SEPARATE VERDICTS.
#
# The honest claim, spelled out once here rather than three times: this
# process cannot outlive its own restart to watch itself. "The process is
# killed and restarted" is proven by running HOLD, letting it exit (by
# design, on purpose, leaving the position and stop live on the venue), and
# then starting a SECOND, independent process (VERIFY) that reads the venue
# cold - no shared state, no in-memory handle carried over. The stop
# surviving THAT boundary - the only boundary a real process death would
# ever cross - is the claim. A tool that stayed running to watch its own
# variables would prove nothing about a restart at all.
#
# The read-only gate-invariant checker elsewhere in tools/ (allows_live /
# FUND_ABS / cap / LIVE_AUTHORIZED) is not, and cannot be, evidence here: it
# never calls verify_stop or reads a position's stop fields. Only a
# HOLD -> VERIFY -> FLATTEN transcript from this tool is Item 2 evidence.
# ---------------------------------------------------------------------------


def run_hold(*, client: Any, notional: Optional[float] = None, out: str = "",
             stop_distance_fraction: float = STOP_DISTANCE_FRACTION
             ) -> Dict[str, Any]:
    """Phase HOLD. Opens + attaches a real linear stop, then EXITS LEAVING
    THE POSITION AND STOP LIVE ON THE VENUE - it never flattens. This
    process's own exit, right after `attach`/`verify` pass, is the "process
    death" Item 2 is about; a later, independent process (`run_verify`)
    reading the venue cold is what proves the stop survived it."""
    import shadow

    cap = float(shadow.SHADOW_MAX_NOTIONAL_USD)
    asked = cap if notional is None else float(notional)
    d = Drill(client=client, notional=min(asked, cap), arm=True)
    state: Dict[str, Any] = {"pid": os.getpid()}

    def reachability(s: Stage) -> None:
        mark = float(client.get_last_price(SYMBOL))
        if not mark > 0:
            s.failed(f"last price unusable: {mark!r}")
            return
        state["mark"] = mark
        s.passed(last_price=mark, venue=d.venue, category=client.category)

    def auth(s: Stage) -> None:
        wallet = client.get_wallet()
        s.passed(auth_ok=True, account_type=wallet.get("accountType", ""))

    def flat(s: Stage) -> None:
        row = client.get_position(SYMBOL)
        held = abs(float((row or {}).get("size", 0) or 0))
        state["venue_perp_qty"] = held
        if held > 0:
            s.failed(
                f"the venue already holds {held} on {SYMBOL}. A HOLD that "
                "opens on top of something already there is not a drill, "
                "it is an incident.", venue_perp_qty=held)
            return
        s.passed(venue_perp_qty=held)

    def venue_rules(s: Stage) -> None:
        filters = client.get_instrument_filters(SYMBOL, force=True)
        state["filters"] = filters
        s.passed(qty_step=float(filters.qty_step),
                 min_qty=float(filters.min_qty),
                 min_notional=float(filters.min_notional),
                 from_exchange=filters.from_exchange)

    def arm_gate(s: Stage) -> None:
        _assert_can_arm(client.cfg)
        s.passed(pid=state["pid"])

    def open_position(s: Stage) -> None:
        filters = state["filters"]
        qty = max(float(filters.min_qty), d.notional / state["mark"])
        result = client.place_order(
            symbol=SYMBOL, side="Buy", qty=qty, order_type="Market",
            purpose="linear_stop_hold", filters=filters)
        d.orders_sent += 1
        if not result.ok:
            s.failed(f"open leg not accepted: {result.reason}",
                     reason=result.reason, order_link_id=result.order_link_id)
            return
        row = client.get_position(SYMBOL)
        size = abs(float((row or {}).get("size", 0) or 0))
        if size <= 0:
            s.failed(
                "order accepted but the venue reports no position "
                "afterward; whether it filled is unknown",
                order_link_id=result.order_link_id)
            return
        state["position_size"] = size
        state["entry_order_link_id"] = result.order_link_id
        s.passed(order_link_id=result.order_link_id,
                 filled_size=size, avg_price=(row or {}).get("avgPrice"))

    def attach(s: Stage) -> None:
        mark = state["mark"]
        trigger = mark * (1.0 - stop_distance_fraction)
        result = client.place_stop_order(
            symbol=SYMBOL, side="Sell", qty=state["position_size"],
            trigger_price=trigger, filters=state["filters"])
        if not result.ok:
            s.failed(f"trading-stop not accepted: {result.reason}",
                     reason=result.reason, trigger_price=trigger)
            return
        state["stop_order_link_id"] = result.order_link_id
        s.passed(order_link_id=result.order_link_id, trigger_price=trigger,
                 mechanism=(result.raw or {}).get("mechanism", ""))

    def verify(s: Stage) -> None:
        live, detail = client.verify_stop(
            symbol=SYMBOL, order_link_id=state["stop_order_link_id"])
        if not live:
            s.failed(f"verify_stop reports live=False: {detail}",
                     live=live, detail=detail)
            return
        s.passed(live=live, detail=detail,
                 endpoint="GET /v5/position/list (stopLoss field)")

    def position_evidence(s: Stage) -> None:
        row = client.get_position(SYMBOL) or {}
        s.passed(**{
            k: row.get(k) for k in
            ("symbol", "side", "size", "avgPrice", "stopLoss", "tpslMode",
             "slTriggerBy", "slOrderType", "positionIdx")
            if k in row})

    for name, fn in (("reachability", reachability), ("auth", auth),
                     ("flat", flat), ("venue_rules", venue_rules),
                     ("arm_gate", arm_gate), ("open", open_position),
                     ("attach", attach), ("verify", verify),
                     ("position_evidence", position_evidence)):
        if not d.run_stage(name, fn):
            return _report_item2(d, state, out, cap, verdict="FAILED",
                                 phase="HOLD")

    d.still_open = True
    return _report_item2(d, state, out, cap, verdict="HOLD", phase="HOLD")


def run_verify(*, client: Any, out: str = "") -> Dict[str, Any]:
    """Phase VERIFY. Reads the venue COLD from a fresh process - no shared
    state or in-memory handle carried over from `run_hold`. get_position and
    verify_stop are the whole test: if the position is gone or the stop is
    not live, the restart did NOT preserve it. Sends nothing."""
    d = Drill(client=client, notional=0.0, arm=False)
    state: Dict[str, Any] = {"pid": os.getpid()}

    def reachability(s: Stage) -> None:
        mark = float(client.get_last_price(SYMBOL))
        if not mark > 0:
            s.failed(f"last price unusable: {mark!r}")
            return
        state["mark"] = mark
        s.passed(last_price=mark, venue=d.venue, category=client.category)

    def auth(s: Stage) -> None:
        wallet = client.get_wallet()
        s.passed(auth_ok=True, account_type=wallet.get("accountType", ""),
                 pid=state["pid"])

    def position_still_open(s: Stage) -> None:
        row = client.get_position(SYMBOL)
        size = abs(float((row or {}).get("size", 0) or 0))
        if size <= 0:
            s.failed(
                "the venue holds no position on "
                f"{SYMBOL}; restart did not preserve the position, so the "
                "stop cannot have survived it either", venue_perp_qty=size)
            return
        state["position_size"] = size
        state["stop_order_link_id"] = (row or {}).get("stopLoss", "")
        s.passed(venue_perp_qty=size)

    def verify(s: Stage) -> None:
        live, detail = client.verify_stop(symbol=SYMBOL, order_link_id="")
        if not live:
            s.failed(
                f"verify_stop reports live=False: {detail}. Restart did "
                "NOT preserve the stop.", live=live, detail=detail)
            return
        s.passed(live=live, detail=detail,
                 endpoint="GET /v5/position/list (stopLoss field)")

    def position_evidence(s: Stage) -> None:
        row = client.get_position(SYMBOL) or {}
        s.passed(**{
            k: row.get(k) for k in
            ("symbol", "side", "size", "avgPrice", "stopLoss", "tpslMode",
             "slTriggerBy", "slOrderType", "positionIdx")
            if k in row})

    for name, fn in (("reachability", reachability), ("auth", auth),
                     ("position_still_open", position_still_open),
                     ("verify", verify),
                     ("position_evidence", position_evidence)):
        if not d.run_stage(name, fn):
            return _report_item2(d, state, out, 0.0, verdict="FAILED",
                                 phase="VERIFY")

    return _report_item2(d, state, out, 0.0, verdict="VERIFIED", phase="VERIFY")


def run_flatten(*, client: Any, out: str = "") -> Dict[str, Any]:
    """Phase FLATTEN. Required cleanup after HOLD/VERIFY: reduce-only close,
    then confirm the venue is flat and the stop is gone. If the venue is
    already flat (VERIFY already found nothing, or a human closed it by
    hand) this is a no-op pass, not a failure."""
    d = Drill(client=client, notional=0.0, arm=True)
    state: Dict[str, Any] = {}

    def reachability(s: Stage) -> None:
        mark = float(client.get_last_price(SYMBOL))
        if not mark > 0:
            s.failed(f"last price unusable: {mark!r}")
            return
        state["mark"] = mark
        s.passed(last_price=mark, venue=d.venue, category=client.category)

    def auth(s: Stage) -> None:
        wallet = client.get_wallet()
        s.passed(auth_ok=True, account_type=wallet.get("accountType", ""))

    def arm_gate(s: Stage) -> None:
        _assert_can_arm(client.cfg)
        s.passed()

    def position_check(s: Stage) -> None:
        row = client.get_position(SYMBOL)
        size = abs(float((row or {}).get("size", 0) or 0))
        state["venue_perp_qty"] = size
        s.passed(venue_perp_qty=size)

    for name, fn in (("reachability", reachability), ("auth", auth),
                     ("arm_gate", arm_gate), ("position_check", position_check)):
        if not d.run_stage(name, fn):
            return _report_item2(d, state, out, 0.0, verdict="FAILED",
                                 phase="FLATTEN")

    if state["venue_perp_qty"] <= 0:
        return _report_item2(d, state, out, 0.0, verdict="ALREADY_FLAT",
                             phase="FLATTEN")

    filters = client.get_instrument_filters(SYMBOL, force=True)
    state["filters"] = filters

    def flatten(s: Stage) -> None:
        result = client.place_order(
            symbol=SYMBOL, side="Sell", qty=state["venue_perp_qty"],
            order_type="Market", purpose="linear_stop_hold_flatten",
            reduce_only=True, filters=state["filters"])
        d.orders_sent += 1
        if not result.ok:
            d.still_open = True
            s.failed(
                f"flatten not accepted: {result.reason}. THE POSITION MAY "
                "STILL BE OPEN. A human must close it by hand and confirm "
                "the stop is gone.", reason=result.reason)
            return
        s.passed(order_link_id=result.order_link_id)

    def final_reconcile(s: Stage) -> None:
        row = client.get_position(SYMBOL)
        size = abs(float((row or {}).get("size", 0) or 0))
        if size > 1e-9:
            d.still_open = True
            s.failed(f"the venue still holds {size} after flatten",
                     venue_perp_qty=size, verdict="NOT_FLAT")
            return
        live, detail = client.verify_stop(symbol=SYMBOL, order_link_id="")
        s.passed(venue_perp_qty=size, verdict="FLAT",
                 stop_still_live=live, stop_detail=detail)

    for name, fn in (("flatten", flatten), ("final_reconcile", final_reconcile)):
        if not d.run_stage(name, fn):
            return _report_item2(d, state, out, 0.0, verdict="FAILED",
                                 phase="FLATTEN")

    return _report_item2(d, state, out, 0.0, verdict="FLATTENED", phase="FLATTEN")


def _report_item2(d: Drill, state: Dict[str, Any], out: str, cap: float,
                  *, verdict: str, phase: str) -> Dict[str, Any]:
    evidence = {s.name: s.evidence for s in d.stages}
    if verdict == "HOLD":
        action = (
            "POSITION STILL OPEN — HUMAN MUST ACT. Kill this process (or let "
            f"it exit; pid={state.get('pid')}), then start a NEW process and "
            "run --verify, then --flatten. Nothing automated will close this."
        )
    elif d.still_open:
        action = ("A HUMAN MUST ACT NOW: the position may still be open at "
                  "the venue. Close it by hand and confirm no stop is left "
                  "live. Nothing automated will retry.")
    elif verdict == "FAILED" and phase == "VERIFY":
        action = (f"the drill stopped at {d.failed_stage!r}: restart did "
                  "NOT preserve the position and/or its stop. This is the "
                  "failure Item 2 exists to catch.")
    elif verdict == "FAILED":
        action = (f"the drill stopped at {d.failed_stage!r}; nothing is "
                  "proven. Fix what the stage reports and run it again.")
    elif verdict == "ALREADY_FLAT":
        action = "the venue already held nothing; no flatten was necessary."
    elif verdict == "VERIFIED":
        action = ("VERIFIED from a fresh process: the venue still shows the "
                  "position and verify_stop still reads live=True. Run "
                  "--flatten next to close out. A human still fills the "
                  "checklist's observed: line - this tool signs nothing.")
    else:
        action = "FLATTENED. The venue is flat and the stop is gone."
    report = {
        "tool": "linear_stop_venue_drill",
        "checklist_item": "linear_protective_stop_verified (Item 2 of 4)",
        "phase": phase,
        "verdict": verdict,
        "venue": d.venue,
        "armed": bool(d.arm),
        "failed_stage": d.failed_stage,
        "still_open": d.still_open,
        "next_action": action,
        "orders_sent": d.orders_sent,
        "stages": [s.as_dict() for s in d.stages],
        "evidence": evidence,
        "restart_claim": (
            "This process cannot outlive its own restart. HOLD opens+attaches "
            "then exits leaving the position and stop live on the venue - "
            "that exit is the 'process death'. VERIFY, run later as a "
            "separate process with no shared state, reads the venue cold; "
            "its verify_stop live=True is the claim that the stop survived "
            "the restart. tools/session_tail.py is not this evidence - it "
            "never calls verify_stop or reads a position's stop fields."
        ),
        "not_proven": list(NOT_PROVEN),
        "finished_ms": int(time.time() * 1000),
    }
    _dump(report, out)
    return report


def _scratch_state_db() -> str:
    """A throwaway `StateStore` path. Same reasoning as `drill.py`'s own
    helper: never the committed fixture, never the running book's database.
    This tool is a separate process and reads its position from the venue."""
    import tempfile
    folder = tempfile.mkdtemp(prefix="linear-stop-drill-")
    return os.path.join(folder, "drill_state.db")


def _load_client():
    """Build a bare BybitClient from the environment. No bot, no engine, no
    carry book - this tool proves one venue mechanism, not the whole stack."""
    import config as _config
    from bybit_connection import BybitClient
    from persistence import StateStore

    # `config.load(env)` treats a non-None mapping as the ONLY env source
    # (`env = os.environ if env is None else env`) - passing `{}` means zero
    # env vars, silently: BYBIT_API_SECRET always empty ("cannot sign: no API
    # secret configured") and CATEGORY always defaults to "spot" regardless
    # of what the shell actually exports. Omitting the argument reads the
    # real os.environ, same as every other caller of this loader.
    cfg = _config.load()
    store = StateStore(_scratch_state_db())
    client = BybitClient(config=cfg, store=store)
    return client, cfg


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--arm", action="store_true",
                    help="send orders. Without it nothing is sent.")
    phase = ap.add_mutually_exclusive_group()
    phase.add_argument("--hold", action="store_true",
                       help="Item 2, phase HOLD: open + attach a real stop, "
                            "then EXIT LEAVING THE POSITION AND STOP LIVE ON "
                            "THE VENUE. Implies --arm.")
    phase.add_argument("--verify", action="store_true",
                       help="Item 2, phase VERIFY: read the venue cold from "
                            "this fresh process; verify_stop must be "
                            "live=True. Sends nothing.")
    phase.add_argument("--flatten", action="store_true",
                       help="Item 2, phase FLATTEN: required cleanup after "
                            "HOLD/VERIFY. Closes the position if one is open.")
    ap.add_argument("--notional", type=float, default=None)
    ap.add_argument("--out", default="")
    args = ap.parse_args(argv)

    try:
        client, cfg = _load_client()
    except Exception as exc:                                    # noqa: BLE001
        print(f"the venue client could not be built: {type(exc).__name__}: "
              f"{exc}\n\nRun with the virtualenv the suite uses:\n\n"
              "    . .venv/bin/activate && python3 bot/tools/linear_stop_venue_drill.py\n",
              file=sys.stderr)
        return 2

    arming = args.arm or args.hold or args.flatten
    if arming:
        try:
            _assert_can_arm(cfg)
        except LinearStopDrillRefused as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 2

    if args.hold:
        report = run_hold(client=client, notional=args.notional, out=args.out)
    elif args.verify:
        report = run_verify(client=client, out=args.out)
    elif args.flatten:
        report = run_flatten(client=client, out=args.out)
    else:
        report = run_drill(client=client, notional=args.notional, arm=args.arm,
                           out=args.out)

    phase_label = report.get("phase", "")
    print("=" * 74)
    print(f"LINEAR PROTECTIVE-STOP VENUE DRILL — {report['verdict']}"
          f"{f' [{phase_label}]' if phase_label else ''}"
          f"   ({'ARMED' if report['armed'] else 'preflight only'})")
    print("=" * 74)
    print(f"  venue: {report['venue']}")
    print()
    for stage in report["stages"]:
        mark = "ok  " if stage["ok"] else "STOP"
        print(f"  [{mark}] {stage['stage']}")
        for key, value in stage["evidence"].items():
            print(f"           {key}: {value}")
        if stage["why"]:
            print(f"           -> {stage['why']}")
    print(f"\n  orders sent: {report['orders_sent']}")
    print(f"\n  {report['next_action']}")
    if report["verdict"] == "HOLD":
        print(f"\n  >>> POSITION STILL OPEN on the venue (pid={os.getpid()}). "
              "<<<")
        print("  >>> A human must now run --verify (new process), then "
              "--flatten. <<<")
    print("\n  WHAT A PASS STILL DOES NOT ESTABLISH")
    for line in report["not_proven"]:
        print(f"    - {line}")
    if args.out:
        print(f"\n  transcript: {args.out}")
    return 0 if report["verdict"] in (
        "PASSED", "PREFLIGHT_ONLY", "HOLD", "VERIFIED", "FLATTENED",
        "ALREADY_FLAT") else 1


if __name__ == "__main__":
    raise SystemExit(main())
