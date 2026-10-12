#!/usr/bin/env python3
"""Read-only TESTNET preflight for the carry book. It cannot send an order.

Run it before the first order-capable carry run, from the environment that holds
the testnet credentials. It reads exactly what the carry book reads at cold start
(through the book's own `CarryBroker`, not the generic client, whose
`get_position` is linear-only), and answers one question: is it safe and
meaningful to let the book run?

SAFETY, STRUCTURALLY
  * the broker is built with an order gate that refuses every order, so even a
    future edit that called `place_*` here would raise `CarryOrderRefused`;
  * it refuses to run unless the loaded config is TESTNET and no live
    authorisation is present, and it never sets `LIVE_TRADING_ACK`;
  * it prints no key, secret or signature.

THREE SEPARATE QUESTIONS, THREE SEPARATE ANSWERS
  1. READINESS      PREFLIGHT_OK | PREFLIGHT_BLOCKED   (exit 0 | 2)
     Could the venue be read, and do the safety preconditions hold?
  2. ENTRY ECONOMICS  ENTRY_ECONOMICS_PASS | _REFUSED | _UNAVAILABLE
     What the PRODUCTION entry-cost evaluator says about opening a pair right
     now. This is `CarryEngine.entry_economics`, the very method `on_candle`
     calls, fed the same funding history, fee rates, execution mode/style and
     borrow rate; it is not a copy of the formula. UNAVAILABLE means an input was
     unreadable or stale, which the engine also treats as a refusal.
  3. ORDER AUTHORISATION  main.carry_orders_permitted, the carry order gate.
     Reported, never decided here.
A successful readiness check is NOT a profitable entry, an economics pass is NOT
permission to trade, and neither can override the order gate or the runtime risk
checks. Whether a qualifying opportunity exists at run time is the engine's call.

Exit codes: 0 PREFLIGHT_OK | 2 PREFLIGHT_BLOCKED | 3 PREFLIGHT_REFUSED (wrong
environment, nothing was read) | 5 with --require-economics-pass when readiness
is fine but the entry economics are not a pass.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from typing import Any, Callable, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

#: Flat to within this many base units counts as flat.
FLAT_DUST = 1e-9


def _check(name: str, fn: Callable[[], Any]) -> Dict[str, Any]:
    """Run one read. An unreadable venue is a BLOCKER, never an assumption."""
    try:
        return {"name": name, "ok": True, "value": fn()}
    except Exception as exc:  # noqa: BLE001
        return {"name": name, "ok": False,
                "error": f"{type(exc).__name__}: {exc}"}


DAY_S = 86_400.0


def assess_entry_economics(*, broker: Any, spot_symbol: str, perp_symbol: str,
                           execution_mode: str, execution_style: Optional[str],
                           borrow_apr: Optional[float],
                           now_s: Optional[float] = None) -> Dict[str, Any]:
    """What the production entry evaluator says right now. Read-only.

    Builds a `CarryEngine` over the (order-refusing) broker, seeds its funding
    history the way `TradingBot` does at startup, takes the market view the way
    `tick` does (including its freshness check), and asks the engine's own
    `entry_economics`. Nothing is placed and `on_candle` is never called.
    """
    import time as _time

    import carry_costs
    from carry_engine import CarryEngine
    from market_snapshot import take_snapshot

    now = _time.time() if now_s is None else float(now_s)
    inputs = {
        "execution_mode": execution_mode, "execution_style": execution_style,
        "borrow_apr": borrow_apr,
        "assumed_hold_days": carry_costs.ASSUMED_HOLD_DAYS,
        "ewma_min_prints": carry_costs.EWMA_MIN_PRINTS,
        "ewma_alpha": carry_costs.EWMA_ALPHA,
        "funding_periods_per_day": carry_costs.FUNDING_PERIODS_PER_DAY,
    }
    out: Dict[str, Any] = {"state": "ENTRY_ECONOMICS_UNAVAILABLE",
                           "inputs": inputs, "reason": "", "history": None,
                           "market_view": None, "evaluator": None}

    def unavailable(reason: str) -> Dict[str, Any]:
        out["state"], out["reason"] = "ENTRY_ECONOMICS_UNAVAILABLE", reason
        return out

    if borrow_apr is None or execution_style is None:
        return unavailable("borrow_apr / execution_style not configured; the "
                           "engine refuses to be built without them")
    try:
        engine = CarryEngine(
            broker=broker, spot_symbol=spot_symbol, perp_symbol=perp_symbol,
            borrow_apr=float(borrow_apr), execution_mode=execution_mode,
            execution_style=execution_style, persist=lambda state: None)
    except Exception as exc:  # noqa: BLE001
        return unavailable(f"engine could not be built: {exc}")

    # The funding history, exactly as startup seeds it (8 settled prints).
    try:
        prints = list(broker.get_funding_history(perp_symbol,
                                                 limit=engine.FUNDING_HISTORY))
    except Exception as exc:  # noqa: BLE001
        out["history"] = {"readable": False, "error": f"{type(exc).__name__}: {exc}"}
        return unavailable("funding history unreadable; the engine would stand "
                           "aside with INSUFFICIENT_FUNDING_HISTORY")
    stamps = sorted(int(ms) for _bps, ms in prints)
    out["history"] = {
        "readable": True, "prints": len(prints),
        "required_for_ewma": carry_costs.EWMA_MIN_PRINTS,
        "enough_for_ewma": len(prints) >= carry_costs.EWMA_MIN_PRINTS,
        "oldest_ms": stamps[0] if stamps else None,
        "newest_ms": stamps[-1] if stamps else None,
        "span_days": (round((stamps[-1] - stamps[0]) / 86_400_000, 2)
                      if stamps else 0.0),
        "newest_age_hours": (round((now * 1000 - stamps[-1]) / 3_600_000, 2)
                             if stamps else None),
        "source": "GET /v5/market/funding/history (public)",
    }
    engine.warm_funding_history(prints)

    # The market view, exactly as tick takes it, including the freshness check.
    try:
        view = take_snapshot(broker, perp_symbol=perp_symbol,
                             spot_symbol=spot_symbol, now_s=now)
        view.assert_fresh()
    except Exception as exc:  # noqa: BLE001
        out["market_view"] = {"usable": False,
                              "error": f"{type(exc).__name__}: {exc}"}
        return unavailable("market view unusable; tick() would not act on it "
                           "and neither does this assessment")
    out["market_view"] = {
        "usable": True, "source": "take_snapshot (public tickers + funding)",
        "observed_age_s": round(view.age_s, 2),
        "perp_mark": view.perp_mark, "spot_mark": view.spot_mark,
        "basis_bps": round(view.basis_bps, 3),
        "last_settled_funding_bps": view.funding_print_bps,
        "last_settled_age_hours": (
            None if view.funding_print_ms is None else
            round((now * 1000 - view.funding_print_ms) / 3_600_000, 2)),
        "venue_clock_skew_s": view.clock_skew_s,
    }

    econ = engine.entry_economics(
        mark=view.perp_mark, spot=view.spot_mark,
        funding_bps=float(view.funding_print_bps or 0.0))
    out["round_trip_bps"] = econ.round_trip_bps
    v = econ.verdict
    if v is not None:
        out["evaluator"] = {
            "allowed": bool(v.allowed), "reason": v.reason,
            "smoothed_funding_bps_per_print": v.smoothed_funding_bps,
            "expected_carry_bps_per_day": v.expected_carry_bps_per_day,
            "borrow_bps_per_day": v.borrow_bps_per_day,
            "amortised_round_trip_bps_per_day":
                v.amortised_round_trip_bps_per_day,
            "net_edge_bps_per_day": v.net_edge_bps_per_day,
            "entry_basis_bps": v.entry_basis_bps,
            "basis_budget_bps": v.basis_budget_bps, "detail": v.detail,
        }
    if econ.refusal is None:
        out["state"], out["reason"] = (
            "ENTRY_ECONOMICS_PASS", v.reason if v is not None else "")
    elif v is None:
        # The engine refused before the evaluator ran (no spot, unpriced fees).
        return unavailable(f"{econ.refusal.reason}: "
                           f"{econ.refusal.detail.get('note', '')}")
    else:
        out["state"], out["reason"] = "ENTRY_ECONOMICS_REFUSED", v.reason
    return out


def run_preflight(*, broker: Any, client: Any, cfg: Any, spot_symbol: str,
                  perp_symbol: str, execution_mode: str,
                  known_perp_qty: float = 0.0,
                  execution_style: Optional[str] = None,
                  borrow_apr: Optional[float] = None,
                  now_s: Optional[float] = None) -> Dict[str, Any]:
    """Pure function over injected collaborators, so it is testable offline.

    ``known_perp_qty`` is what the local ledger believes the perp holds (0.0 on a
    fresh state database). A venue position the ledger does not know about is a
    blocker: the book refuses to start on that mismatch (`carry_cold_start`).
    """
    reads: Dict[str, Dict[str, Any]] = {}
    reads["equity"] = _check("equity", client.get_equity)
    reads["perp_position"] = _check(
        "perp_position", lambda: broker.get_perp_position(perp_symbol))
    reads["spot_inventory"] = _check(
        "spot_inventory", lambda: broker.get_spot_inventory(spot_symbol))
    reads["open_carry_orders"] = _check(
        "open_carry_orders",
        lambda: broker.get_open_carry_orders(spot_symbol, perp_symbol))
    reads["lot_perp"] = _check(
        "lot_perp", lambda: broker.get_lot_rules(perp_symbol, "linear"))
    reads["fee_perp"] = _check(
        "fee_perp", lambda: broker.get_fee_rates(perp_symbol, "linear"))
    reads["fee_spot"] = _check(
        "fee_spot", lambda: broker.get_fee_rates(spot_symbol, "spot"))
    reads["mark_perp"] = _check("mark_perp", lambda: broker.get_mark(perp_symbol))
    reads["mark_spot"] = _check("mark_spot",
                                lambda: broker.get_spot_mark(spot_symbol))
    reads["funding_bps"] = _check(
        "funding_bps", lambda: broker.get_funding_bps(perp_symbol))

    blockers: List[str] = []
    notes: List[str] = []
    for name, r in reads.items():
        if not r["ok"]:
            blockers.append(f"{name} unreadable: {r['error']}")

    def val(name: str) -> Any:
        return reads[name].get("value")

    perp = val("perp_position")
    if perp is not None and abs(perp - known_perp_qty) > max(
            FLAT_DUST, 1e-6 * max(abs(known_perp_qty), 1.0)):
        blockers.append(
            f"venue perp position {perp} != the {known_perp_qty} the local "
            "ledger knows about; the book refuses to start on this mismatch "
            "and so should you")
    orders = val("open_carry_orders")
    if orders:
        blockers.append(f"{len(orders)} open carry order(s) at the venue: "
                        f"{[o.get('orderLinkId') for o in orders]}")

    inventory, lot = val("spot_inventory"), val("lot_perp")
    if execution_mode == "overlay" and inventory is not None and lot:
        if inventory + FLAT_DUST < lot["min_qty"]:
            blockers.append(
                f"overlay mode hedges BTC the account already owns, and it holds "
                f"{inventory} (< the perp minimum {lot['min_qty']}); the book "
                "cannot open and will not buy or sell your spot to change that")
    equity = val("equity")
    if equity is not None and equity <= 0:
        blockers.append(f"equity is {equity}")

    # The REAL carry order gate (main.carry_orders_permitted, the function
    # build_bot hands to CarryBroker), not a copy of its logic.
    from main import carry_orders_permitted
    gate_open, gate_reason = carry_orders_permitted(cfg)
    if not gate_open:
        notes.append(
            "PAPER_TRADING=1: with this environment the carry order gate "
            "refuses every order, so a run would place NOTHING. Set "
            "PAPER_TRADING=0 (USE_TESTNET stays 1) only for the order-capable "
            "run.")
    economics = assess_entry_economics(
        broker=broker, spot_symbol=spot_symbol, perp_symbol=perp_symbol,
        execution_mode=execution_mode, execution_style=execution_style,
        borrow_apr=borrow_apr, now_s=now_s)
    notes.append("PREFLIGHT_OK means the reads and safety preconditions "
                 "completed. It is not a profitable entry, and an economics "
                 "pass is not permission to trade: orders are controlled only "
                 "by the carry order gate and the runtime risk checks.")

    return {
        "verdict": "PREFLIGHT_BLOCKED" if blockers else "PREFLIGHT_OK",
        "venue": str(getattr(cfg, "BYBIT_VENUE", "testnet")),
        "use_testnet": bool(cfg.USE_TESTNET),
        "paper_trading": bool(cfg.PAPER_TRADING),
        "order_gate_would_allow_orders": bool(gate_open),
        "order_authorization": {
            "gate_allows_orders": bool(gate_open), "reason": gate_reason,
            "controlled_by": "main.carry_orders_permitted (the carry order "
                             "gate) and the runtime risk checks; nothing in "
                             "this report authorises an order"},
        "entry_economics": economics,
        "summary": {
            "readiness": "PREFLIGHT_BLOCKED" if blockers else "PREFLIGHT_OK",
            "entry_economics": (economics["state"] + ":" + economics["reason"]
                                if economics["reason"] else economics["state"]),
            "order_gate": ("ALLOWS_ORDERS" if gate_open else "REFUSES_ORDERS")
                          + ":" + str(gate_reason),
        },
        "execution_mode": execution_mode,
        "symbols": {"spot": spot_symbol, "perp": perp_symbol},
        "reads": {k: (v["value"] if v["ok"] else {"ERROR": v["error"]})
                  for k, v in reads.items()},
        "blockers": blockers,
        "notes": notes,
    }


def load_testnet_config():
    """``(cfg, None)`` for a testnet, non-live environment, else ``(None,
    (json_text, exit_code))``. Shared by every read-only testnet tool so none
    of them can be pointed at mainnet or run with a live authorisation.
    """
    try:
        # `config` loads (and validates) the environment at IMPORT time, so the
        # import itself is where a contradictory environment is refused.
        import config as _config
        cfg = _config.load()
    except Exception as exc:  # noqa: BLE001
        if type(exc).__name__ != "ConfigError":
            raise
        return None, (json.dumps({
            "verdict": "PREFLIGHT_REFUSED",
            "reason": f"configuration refused: {exc}"}), 3)
    if not cfg.USE_TESTNET:
        return None, (json.dumps({
            "verdict": "PREFLIGHT_REFUSED",
            "reason": "USE_TESTNET is not set; this tool is testnet-only and "
                      "read nothing"}), 3)
    armed, _why = _config.is_live_authorized(cfg)
    if armed is True or getattr(cfg, "LIVE_TRADING_ACK", ""):
        return None, (json.dumps({
            "verdict": "PREFLIGHT_REFUSED",
            "reason": "a live authorisation is present in this environment; "
                      "unset it. Nothing was read"}), 3)
    return cfg, None


def exit_code(report: Dict[str, Any], *, require_economics_pass: bool) -> int:
    """Readiness decides 0/2. Economics only matters if the operator asks."""
    if report["verdict"] != "PREFLIGHT_OK":
        return 2
    if require_economics_pass and \
            report["entry_economics"]["state"] != "ENTRY_ECONOMICS_PASS":
        return 5
    return 0


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--require-economics-pass", action="store_true",
                    help="exit 5 when readiness is fine but the production "
                         "entry economics are not ENTRY_ECONOMICS_PASS")
    ap.add_argument("--db", default=None,
                    help="state database to compare against (read for the "
                         "ledger's perp quantity); default: a throwaway file")
    args = ap.parse_args(argv)

    cfg, refusal = load_testnet_config()
    if refusal is not None:
        print(refusal[0])
        return refusal[1]

    import persistence
    from carry_broker import CarryBroker
    import bybit_connection as bc

    db = args.db or os.path.join(tempfile.mkdtemp(prefix="carry_preflight_"),
                                 "state.db")
    store = persistence.StateStore(db)
    try:
        client = bc.BybitClient(config=cfg, store=store)
        broker = CarryBroker(
            client=client, sequence_source=lambda *a: 0,
            order_gate=lambda: (False, "preflight is read-only"))
        known = 0.0
        try:
            import ledger as _ledger
            rows = store.load_ledger()
            if rows:
                known = abs(_ledger.Journal.from_rows(rows)
                            .statement()["open_perp_qty"])
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"verdict": "PREFLIGHT_BLOCKED",
                              "blockers": [f"local ledger unreadable: {exc}"]}))
            return 2
        report = run_preflight(
            broker=broker, client=client, cfg=cfg,
            spot_symbol=cfg.CARRY_SPOT_SYMBOL, perp_symbol=cfg.CARRY_PERP_SYMBOL,
            execution_mode=str(cfg.CARRY_EXECUTION_MODE),
            known_perp_qty=known,
            execution_style=cfg.CARRY_EXECUTION_STYLE,
            borrow_apr=cfg.CARRY_BORROW_APR)
    finally:
        store.close()
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return exit_code(report, require_economics_pass=args.require_economics_pass)


if __name__ == "__main__":
    sys.exit(main())
