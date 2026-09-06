"""Run a short paper session against the offline exchange stub and print it.

WHAT THIS IS FOR
================
`docs/PAPER_RUNBOOK.md` claims that `ENTRIES_ENABLED=0` produces a healthy
process that submits zero orders. This runs that claim and prints the resulting
session record, so an operator can see it rather than take it on trust — without
credentials, without a network, and without touching a real exchange.

It uses `tests/fake_bybit.FakeBybit`, the same strict offline stub the test suite
uses: it validates HMAC signatures and rejects requests the real exchange would
reject.

WHAT IT IS NOT
==============
It is not a backtest, not a performance demo, and not evidence of anything about
edge. It prints no PnL because the session record contains none, by design.
Timing-skill research is CLOSED with an ABSENT verdict for both signal families
measured — see `RESEARCH_CLOSE_STAGE1.md`.

USAGE
=====
    python3 tools/paper_session_demo.py                  # stand-down, entries off
    python3 tools/paper_session_demo.py --entries-enabled  # paper, entries on
    python3 tools/paper_session_demo.py --ticks 25 --json-out /tmp/session.jsonl

Exit code 0 when the session behaved as the runbook says it should, 1 otherwise.
The check is deliberately narrow: healthy, reconciled, not live-authorised, and —
when entries are disabled — exactly zero orders submitted.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tests"))

import bybit_connection as bc  # noqa: E402
import main as m  # noqa: E402
import session_log as sl  # noqa: E402
import trading_engine as te  # noqa: E402
from fake_bybit import API_KEY, API_SECRET, FakeBybit  # noqa: E402
from persistence import StateStore  # noqa: E402
from position_sizing import BillionairePositionSizing  # noqa: E402
from risk_management import BillionaireRiskManager  # noqa: E402

EQUITY = 100_000.0
ENTRY = 50_000.0


class DemoConfig:
    """Paper + testnet. Deliberately not loaded from the environment.

    A demo that read the real environment could be pointed at mainnet by an
    unlucky shell. These values are fixed in code and the live gate is never
    satisfiable from here.
    """

    USE_TESTNET = True
    PAPER_TRADING = True
    LIVE_AUTHORIZED = False
    LIVE_TRADING_ACK = ""
    POLICY_MODE = "off"
    ENTRIES_ENABLED = False
    PAPER_SESSION_LOG_PATH = ""
    BYBIT_API_KEY = API_KEY
    BYBIT_API_SECRET = API_SECRET
    BYBIT_RECV_WINDOW_MS = 5000
    REQUEST_TIMEOUT_SECONDS = 5.0
    USE_LEVERAGE = False
    SYMBOL_FILTERS_TTL = 3600
    ORDERLINK_PREFIX = "BB"
    MAX_POSITION_SIZE_PCT = 0.02
    MAX_TOTAL_EXPOSURE_PCT = 0.10
    MAX_DAILY_LOSS_PCT = 0.02
    MAX_DRAWDOWN_PCT = 0.10
    STOP_LOSS_PCT = 0.02
    RISK_PER_TRADE_PCT = 0.005
    MAX_OPEN_POSITIONS = 4
    MAX_CONSECUTIVE_LOSSES = 4
    MIN_RISK_REWARD_RATIO = 2.0
    MIN_CONFIDENCE = 0.60
    MAX_CORRELATION_EXPOSURE_PCT = 0.30
    MAX_LEVERAGE_EU = 1
    DEFAULT_LEVERAGE = 1
    TRADE_COOLDOWN_SECONDS = 0
    CIRCUIT_BREAKER_HOURS = 2.0
    BREAKEVEN_AFTER_FIRST_TP = True
    TRADING_SYMBOLS = ("BTCUSDT",)
    LOOP_INTERVAL_SECONDS = 0.0
    ENABLE_HEALTH_SERVER = False
    HEALTHCHECK_PORT = 18199
    MEMORY_ENABLED = True


class DemoStrategy:
    """A signal source that always proposes, so "zero orders" means something."""

    def signal_for(self, symbol):
        return te.TradeIntent(
            symbol=symbol, signal_type="BUY",
            entry_price=ENTRY, stop_price=ENTRY * 0.98,
            take_profits=((ENTRY * 1.05, 1.0),), confidence=0.8,
        )


def run(*, entries_enabled: bool, ticks: int, json_out: str) -> int:
    cfg = DemoConfig()
    cfg.ENTRIES_ENABLED = entries_enabled
    cfg.PAPER_SESSION_LOG_PATH = json_out

    workdir = tempfile.mkdtemp(prefix="paper_demo_")
    exchange = FakeBybit(balances={"USDT": EQUITY, "BTC": 5.0}, equity=EQUITY)
    store = StateStore(os.path.join(workdir, "state.db"))
    store.update_equity(EQUITY)
    client = bc.BybitClient(config=cfg, store=store, transport=exchange)
    risk = BillionaireRiskManager(config=cfg, store=store)
    engine = te.TradingEngine(
        client=client, risk_manager=risk,
        position_sizer=BillionairePositionSizing(config=cfg, risk_manager=risk),
        store=store, config=cfg,
    )
    bot = m.TradingBot(
        config=cfg, store=store, client=client, risk_manager=risk, engine=engine,
        # Entries off means NO signal source, exactly as build_bot decides it.
        strategy=None if not entries_enabled else DemoStrategy(),
    )

    print("=" * 78)
    print("PAPER SESSION DEMO — offline stub, no network, no credentials used")
    print("=" * 78)
    print(f"  ENTRIES_ENABLED : {entries_enabled}")
    print(f"  ticks           : {ticks}")
    print()

    if not bot.startup():
        print("  startup REFUSED — see the log above")
        return 1
    for _ in range(ticks):
        bot.tick()

    health = bot.health()
    record = bot.session.close()

    # Proof the decision path actually ran. Without this, "zero orders" is
    # indistinguishable from "nothing happened at all", which would make the
    # entries-enabled case meaningless.
    decisions = store.recent_decisions(limit=500)
    considered = [d for d in decisions if d["symbol"] != "SESSION"]

    print("  session record")
    print("  " + "-" * 74)
    for key in sorted(record):
        print(f"    {key:<22}: {record[key]}")
    print()
    print(f"  health.healthy        : {health['healthy']}")
    print(f"  exchange order bodies : {len(exchange.order_create_bodies())}")
    print(f"  decisions journalled  : {len(considered)}"
          f"  ({sorted({d['decision'] for d in considered}) or 'none'})")
    print()

    problems = []
    if not health["healthy"]:
        problems.append("process is not healthy")
    if entries_enabled and not considered:
        problems.append(
            "entries were enabled but no decision was journalled — 'zero "
            "orders' would be meaningless")
    if not entries_enabled and considered:
        problems.append(
            f"entries were disabled but {len(considered)} decision(s) were "
            "journalled")
    if not record["reconciled"]:
        problems.append("session did not reconcile")
    if record["live_authorized"]:
        problems.append("live_authorized is true in a paper demo")
    if record["no_edge_claim"] != sl.NO_EDGE_CLAIM:
        problems.append("the NO EDGE CLAIM line is missing or altered")
    if not entries_enabled and record["orders_submitted"] != 0:
        problems.append(
            f"stand-down submitted {record['orders_submitted']} order(s)")
    if record["orders_submitted"] != len(exchange.order_create_bodies()):
        problems.append("the counter disagrees with the exchange stub")

    bot.shutdown()

    if problems:
        for problem in problems:
            print(f"  FAIL: {problem}")
        return 1

    print("  OK — the session behaved as docs/PAPER_RUNBOOK.md describes.")
    print(f"  {sl.NO_EDGE_CLAIM}")
    print("  A clean paper session is evidence the plumbing works. It is NOT")
    print("  evidence of timing skill, which measurement has twice refused to")
    print("  support. See RESEARCH_CLOSE_STAGE1.md.")
    if json_out:
        print(f"\n  session log written to {json_out}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entries-enabled", action="store_true",
                        help="attach a signal source (still paper: zero orders)")
    parser.add_argument("--ticks", type=int, default=10)
    parser.add_argument("--json-out", default="",
                        help="also append the session record here as JSONL")
    args = parser.parse_args(argv)
    return run(entries_enabled=args.entries_enabled, ticks=args.ticks,
               json_out=args.json_out)


if __name__ == "__main__":
    sys.exit(main())
