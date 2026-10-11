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

Verdicts (exit code): PREFLIGHT_OK (0) | PREFLIGHT_BLOCKED (2) | PREFLIGHT_REFUSED (3,
wrong environment, nothing was read).

A BLOCKED verdict names every blocker. "No qualifying opportunity" is NOT a
blocker: the book standing aside is a valid outcome and is not predicted here.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from typing import Any, Callable, Dict, List

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


def run_preflight(*, broker: Any, client: Any, cfg: Any, spot_symbol: str,
                  perp_symbol: str, execution_mode: str,
                  known_perp_qty: float = 0.0) -> Dict[str, Any]:
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

    gate_open = (not bool(cfg.PAPER_TRADING)) and bool(cfg.USE_TESTNET)
    if not gate_open:
        notes.append(
            "PAPER_TRADING=1: with this environment the carry order gate "
            "refuses every order, so a run would place NOTHING. Set "
            "PAPER_TRADING=0 (USE_TESTNET stays 1) only for the order-capable "
            "run.")
    notes.append("whether a qualifying opportunity exists is NOT predicted "
                 "here; the carry gates decide that at run time")

    return {
        "verdict": "PREFLIGHT_BLOCKED" if blockers else "PREFLIGHT_OK",
        "venue": str(getattr(cfg, "BYBIT_VENUE", "testnet")),
        "use_testnet": bool(cfg.USE_TESTNET),
        "paper_trading": bool(cfg.PAPER_TRADING),
        "order_gate_would_allow_orders": gate_open,
        "execution_mode": execution_mode,
        "symbols": {"spot": spot_symbol, "perp": perp_symbol},
        "reads": {k: (v["value"] if v["ok"] else {"ERROR": v["error"]})
                  for k, v in reads.items()},
        "blockers": blockers,
        "notes": notes,
    }


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=None,
                    help="state database to compare against (read for the "
                         "ledger's perp quantity); default: a throwaway file")
    args = ap.parse_args(argv)

    try:
        # `config` loads (and validates) the environment at IMPORT time, so the
        # import itself is where a contradictory environment is refused.
        import config as _config
        cfg = _config.load()
    except Exception as exc:  # noqa: BLE001
        if type(exc).__name__ != "ConfigError":
            raise
        # A contradictory or invalid environment (for example BYBIT_VENUE and
        # USE_TESTNET disagreeing) is a refusal with a reason, not a traceback.
        print(json.dumps({"verdict": "PREFLIGHT_REFUSED",
                          "reason": f"configuration refused: {exc}"}))
        return 3
    if not cfg.USE_TESTNET:
        print(json.dumps({"verdict": "PREFLIGHT_REFUSED",
                          "reason": "USE_TESTNET is not set; this tool is "
                                    "testnet-only and read nothing"}))
        return 3
    armed, _why = _config.is_live_authorized(cfg)
    if armed is True or getattr(cfg, "LIVE_TRADING_ACK", ""):
        print(json.dumps({"verdict": "PREFLIGHT_REFUSED",
                          "reason": "a live authorisation is present in this "
                                    "environment; unset it. Nothing was read"}))
        return 3

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
            known_perp_qty=known)
    finally:
        store.close()
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0 if report["verdict"] == "PREFLIGHT_OK" else 2


if __name__ == "__main__":
    sys.exit(main())
