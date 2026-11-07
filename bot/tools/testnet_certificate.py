"""Offline testnet-qualification certificate and the owner's action manifest. Makes no request, reads no credential, writes nothing.

    python3 tools/testnet_certificate.py [--json]

What it is. Section A runs a fixed battery of probes against the repository's OWN testnet guards with small stubs (temp files only): the drill refuses
mainnet / demo / an unstated or foreign host / a live acknowledgement / paper mode; the live gate is closed for this environment; the kill file, the
cross-process entry limiter and the pure ``testnet_readiness`` gate each refuse a hostile input and accept a positive control. Section B lists the facts only a
VENUE can establish (instrument rules, position mode, margin mode, collateral, fee tier, clock drift, open orders / position, funding interval): each is
``NOT_READ``, never passed. Section C is the bounded plan the owner would authorise.

What it is not. It is not a testnet session and it does not certify one: ``testnet_certified`` is False by construction here, because section B cannot be answered
offline. The offline lifecycle simulator and these probes prove modelled behaviour and guard logic; they are not venue conformance. A testnet result, good or bad,
says nothing about mainnet profitability.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from typing import Any, Dict, List, Mapping, Optional, Sequence

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BOT)
sys.path.insert(0, os.path.join(BOT, "tools"))

import carry_costs as CC  # noqa: E402
import config as C  # noqa: E402
import drill as D  # noqa: E402
import shadow  # noqa: E402
import testnet_readiness as TR  # noqa: E402

PASS, FAIL, NOT_READ = "PASS", "FAIL", "NOT_READ"


class _Cfg:
    def __init__(self, **kw: Any) -> None:
        self.BYBIT_VENUE, self.USE_TESTNET, self.PAPER_TRADING, self.LIVE_TRADING_ACK = "testnet", True, False, ""
        self.__dict__.update(kw)


class _Client:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url


def _probe(name: str, ok: bool, detail: str = "") -> Dict[str, Any]:
    return {"probe": name, "status": PASS if ok else FAIL, "detail": detail}


def section_a(env: Mapping[str, str]) -> List[Dict[str, Any]]:
    from bybit_connection import TESTNET_REST
    out: List[Dict[str, Any]] = []
    armed, why = C.is_live_authorized(dict(env))
    out.append(_probe("live gate is closed in this environment", not armed, why))
    out.append(_probe("LIVE_TRADING_ACK is unset in this environment", not str(env.get("LIVE_TRADING_ACK", "") or "").strip()))
    good = _Client(TESTNET_REST)
    out.append(_probe("positive control: a clean testnet configuration is accepted", D.session_refusal(_Cfg(), good) is None))
    for label, cfg, client in (
            ("drill refuses mainnet", _Cfg(BYBIT_VENUE="mainnet", USE_TESTNET=False), _Client("https://api.bybit.com")),
            ("drill refuses demo", _Cfg(BYBIT_VENUE="demo"), _Client("https://api-demo.bybit.com")),
            ("drill refuses a testnet name on a foreign host", _Cfg(), _Client("https://api-testnet.bybit.com.evil.example")),
            ("drill refuses an unstated host", _Cfg(), _Client("")),
            ("drill refuses a live acknowledgement", _Cfg(LIVE_TRADING_ACK="x"), good),
            ("drill refuses paper mode", _Cfg(PAPER_TRADING=True), good),
            ("drill refuses USE_TESTNET false", _Cfg(USE_TESTNET=False), good)):
        out.append(_probe(label, D.session_refusal(cfg, client) is not None))
    with tempfile.TemporaryDirectory() as td:
        k = os.path.join(td, "KILL")
        out.append(_probe("kill file absent = not engaged", D.kill_file_engaged(k)[0] is False))
        open(k, "w").close()
        out.append(_probe("kill file present = engaged", D.kill_file_engaged(k)[0] is True))
        led = os.path.join(td, "entries.jsonl")
        first, second = D.claim_entry_slot(led, 1_700_000_000_000)[0], D.claim_entry_slot(led, 1_700_000_000_500)[0]
        out.append(_probe("entry limiter grants one armed entry per UTC day and refuses the second", first is True and second is False))
        out.append(_probe("entry limiter refuses when no ledger is named", D.claim_entry_slot("", 1_700_000_000_000)[0] is False))
    # the pure readiness gate: one clean synthetic input (a CONTROL, not venue data) and hostile variants
    inst = {"symbol": "BTCUSDT", "status": "Trading", "fundingInterval": 480, "lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001", "minNotionalValue": "5"},
            "priceFilter": {"tickSize": "0.1"}}
    kw = dict(venue="testnet", use_testnet=True, live_trading=False, live_authorized=False, endpoint_host=TR.TESTNET_HOST, perp_instrument=inst,
              spot_instrument=None, snapshot_ages_s={"perp_mark": 1, "spot_mark": 1, "funding": 1}, venue_clock_offset_ms=10,
              position_mode="one_way", margin_mode="REGULAR_MARGIN", funding_required=False, now_ms=1_700_000_000_000)
    hostile = TR.assess_testnet_readiness(**{**kw, "venue": "mainnet", "use_testnet": False, "position_mode": "hedge", "venue_clock_offset_ms": None})
    out.append(_probe("readiness gate refuses mainnet + hedge mode + unmeasured clock", not hostile.ready and len(hostile.refusals) >= 3))
    bad_live = TR.assess_testnet_readiness(**{**kw, "live_authorized": True})
    out.append(_probe("readiness gate refuses a live-authorised process", not bad_live.ready))
    return out


#: Facts that only the venue can establish. Offline they are NOT_READ, never passed.
VENUE_FACTS = ("the endpoint host answered and is the testnet", "testnet credentials only (no live/mainnet key)", "position mode is one-way",
               "margin mode is stated and allowed", "BTCUSDT linear + BTCUSDT spot instrument rules (tick, step, min notional, status)",
               "funding interval and funding cap of the instrument", "account fee tier for the instrument (fresh, symbol-matched)",
               "collateral available and the BTC the overlay hedges", "no open orders and no unexpected position", "venue clock drift within limit",
               "order history readable (durable order journal can be reconciled to the venue)")


def section_c() -> Dict[str, Any]:
    cap = float(shadow.SHADOW_MAX_NOTIONAL_USD)
    overlay_rt_bps = 2 * CC.TAKER_BPS_PERP
    acquire_rt_bps = CC.ROUND_TRIP_BPS
    return {
        "objective": "exercise order mechanics, reconciliation and restart recovery against the real testnet once, bounded; NOT profitability",
        "instrument": "BTCUSDT linear perpetual (one), overlay mode (hedges BTC already held)",
        "max_notional_usd": cap, "max_entries": 1, "max_duration_hours": 9.0,
        "max_loss_test_currency_usd": round(cap * (overlay_rt_bps / 1e4) + 1.0, 2),
        "max_loss_basis": "taker fees on a round trip (assumed schedule, see below) plus a $1.00 allowance for slippage; a hedge that cannot be unwound is a STOP, not a loss bound",
        "expected_fees_usd": {"overlay_round_trip": round(cap * overlay_rt_bps / 1e4, 4), "acquire_round_trip": round(cap * acquire_rt_bps / 1e4, 4)},
        "fee_inputs": "ASSUMED taker schedule from carry_costs (perp 5.5 bps, spot 10 bps); the testnet account's real tier is read by the preflight and replaces it",
        "data_capture": "execution records (--execution-records), drill evidence file (--evidence-path), cash ledger; keep all three",
        "stop_conditions": ["any stage fails", "a position remains after unwind (HUMAN MUST ACT)", "DRILL_KILL file present", "entry ledger refuses", "more than one entry in the UTC day",
                            "clock drift or stale data refusal"],
        "recovery": "tools/close_testnet_short.py --arm (reduce-only close) then tools/evidence_reconcile.py; a restart reads the venue position, never the local journal",
        "reconciliation": "funding_final + final_reconcile stages: the venue's own position read must say FLAT",
        "report": "drill_out.json + evidence + execution records; summarise with tools/recon_summary.py",
        "authorisation": "NOT GRANTED. The owner gives a separate explicit instruction to run it; nothing in this repository enables the drill or the testnet workflow.",
    }


def build(env: Mapping[str, str]) -> Dict[str, Any]:
    a = section_a(env)
    offline_ok = all(p["status"] == PASS for p in a)
    return {
        "section_a_offline_probes": a, "offline_qualification": PASS if offline_ok else FAIL,
        "section_b_venue_facts": [{"fact": f, "status": NOT_READ} for f in VENUE_FACTS],
        "testnet_certified": False,
        "why_not_certified": "the venue facts in section B can only be established by the owner-run, bounded preflight; the offline probes are guard logic, not venue conformance",
        "section_c_plan": section_c(),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Offline testnet qualification certificate. No request, no credential, no write.")
    ap.add_argument("--json", action="store_true")
    ap.parse_args(argv)
    rep = build(os.environ)
    print(json.dumps(rep, indent=2, sort_keys=True))
    return 0 if rep["offline_qualification"] == PASS else 1


if __name__ == "__main__":
    sys.exit(main())
