"""End-to-end lifecycle tests across config -> risk -> sizing -> client -> engine.

Offline, against the strict Bybit fake. These are the tests that prove the
modules actually compose, rather than each passing in isolation.

The invariant under test throughout:

    A confirmed position always has a verified protective stop.

Several tests deliberately break the exchange mid-lifecycle and assert that the
system ends in a safe state — closed position, kill switch tripped — rather than
a naked one.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bybit_connection as bc  # noqa: E402
import trading_engine as te  # noqa: E402
from fake_bybit import API_KEY, API_SECRET, FakeBybit  # noqa: E402
from persistence import StateStore  # noqa: E402
from position_sizing import BillionairePositionSizing  # noqa: E402
from risk_management import BillionaireRiskManager  # noqa: E402

EQUITY = 100_000.0
ENTRY = 50_000.0


class LiveCfg:
    """Config double with paper mode OFF, so the order path actually runs."""

    USE_TESTNET = True
    PAPER_TRADING = False
    BYBIT_API_KEY = API_KEY
    BYBIT_API_SECRET = API_SECRET
    BYBIT_RECV_WINDOW_MS = 5000
    REQUEST_TIMEOUT_SECONDS = 5.0
    USE_LEVERAGE = False
    SYMBOL_FILTERS_TTL = 3600
    ORDERLINK_PREFIX = "BB"
    # risk limits, all fractions
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
    TRADE_COOLDOWN_SECONDS = 60
    CIRCUIT_BREAKER_HOURS = 2.0
    BREAKEVEN_AFTER_FIRST_TP = True


@pytest.fixture()
def exchange():
    return FakeBybit(balances={"USDT": 100_000.0, "BTC": 5.0}, equity=EQUITY)


@pytest.fixture()
def stack(tmp_path, exchange):
    """The whole stack, wired the way main() wires it."""
    cfg = LiveCfg()
    store = StateStore(str(tmp_path / "state.db"))
    client = bc.BybitClient(config=cfg, store=store, transport=exchange)
    risk = BillionaireRiskManager(config=cfg, store=store)
    sizer = BillionairePositionSizing(config=cfg, risk_manager=risk)
    engine = te.TradingEngine(
        client=client, risk_manager=risk, position_sizer=sizer,
        store=store, config=cfg,
    )
    store.update_equity(EQUITY)
    yield engine, client, risk, store, exchange
    store.close()


def buy_intent(**over):
    base = dict(
        symbol="BTCUSDT",
        signal_type="BUY",
        entry_price=ENTRY,
        stop_price=ENTRY * 0.98,
        # Weighted average = +4%, against a 2% stop -> RR 2.0, which is
        # what MIN_RISK_REWARD_RATIO requires. The gate measures the
        # ladder's expected exit, not its nearest leg.
        take_profits=((ENTRY * 1.024, 0.5), (ENTRY * 1.056, 0.5)),
        confidence=0.75,
    )
    base.update(over)
    return te.TradeIntent(**base)


# ---------------------------------------------------------------------------
# the happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_entry_is_bracketed_and_verified(self, stack):
        engine, client, risk, store, exchange = stack
        report = engine.execute(buy_intent())

        assert report.ok, f"{report.stage}: {report.reason}"
        assert report.reason == "ENTERED_AND_PROTECTED"
        assert report.stop_order_link_id, "no protective stop was recorded"
        assert len(report.take_profit_ids) == 2

        stops = exchange.submitted_orders("stop")
        assert len(stops) == 1
        assert stops[0]["orderFilter"] == "StopOrder"
        assert stops[0]["orderStatus"] == "Untriggered"

    def test_position_is_persisted_with_its_stop(self, stack):
        engine, client, risk, store, exchange = stack
        engine.execute(buy_intent())
        positions = {p["symbol"]: p for p in store.open_positions()}
        assert "BTCUSDT" in positions
        assert float(positions["BTCUSDT"]["stop_price"]) == pytest.approx(ENTRY * 0.98)
        assert store.positions_without_stops() == []

    def test_stop_is_placed_after_the_entry_fills(self, stack):
        """Ordering matters: a stop placed before a fill protects nothing."""
        engine, client, risk, store, exchange = stack
        engine.execute(buy_intent())
        creates = [
            json.loads(r["body"]) for r in exchange.requests
            if r["url"].endswith("/order/create")
        ]
        entry_index = next(
            i for i, b in enumerate(creates) if b.get("orderFilter", "Order") == "Order"
            and b.get("orderType") == "Market"
        )
        stop_index = next(
            i for i, b in enumerate(creates) if b.get("orderFilter") == "StopOrder"
        )
        assert entry_index < stop_index

    def test_size_respects_the_risk_budget_end_to_end(self, stack):
        engine, client, risk, store, exchange = stack
        report = engine.execute(buy_intent())
        notional = report.qty * ENTRY
        assert notional <= EQUITY * LiveCfg.MAX_POSITION_SIZE_PCT * 1.0001

    def test_every_order_carries_a_unique_link_id(self, stack):
        engine, client, risk, store, exchange = stack
        engine.execute(buy_intent())
        ids = [
            json.loads(r["body"])["orderLinkId"] for r in exchange.requests
            if r["url"].endswith("/order/create")
        ]
        assert len(ids) == len(set(ids))
        assert all(len(i) <= 36 for i in ids)

    def test_decision_journal_records_the_entry(self, stack):
        engine, client, risk, store, exchange = stack
        engine.execute(buy_intent())
        decisions = store.recent_decisions()
        assert any(d["decision"] == "ENTERED" for d in decisions)


# ---------------------------------------------------------------------------
# the invariant under failure
# ---------------------------------------------------------------------------


class TestADeadSpotStopIsNotLeftLookingProtected:
    def test_a_rejected_stop_is_reprotected_or_flattened(self, stack):
        import json as _json
        engine, client, risk, store, exchange = stack
        report = engine.execute(buy_intent())
        assert report.ok
        row = store.open_positions()[0]
        meta = _json.loads(row["meta"]) if isinstance(row["meta"], str) else row["meta"]
        stop_id = meta["stop_order_link_id"]
        exchange.orders[stop_id]["orderStatus"] = "Rejected"
        summary = engine.observe_exits()
        assert summary.get("stop_dead") == ["BTCUSDT"]
        # either a fresh live stop was placed, or the position was closed:
        # it is never left "protected" by a stop that is dead
        rows = store.open_positions()
        if rows:
            new_meta = (_json.loads(rows[0]["meta"])
                        if isinstance(rows[0]["meta"], str) else rows[0]["meta"])
            assert new_meta["stop_order_link_id"] != stop_id
        else:
            assert summary["stop_dead_outcome"]["BTCUSDT"] == "FLATTENED"


class TestADeadStopOverPhantomCoin:
    def test_a_dead_stop_with_no_coin_is_not_reprotected_or_sold(self, stack):
        import json as _json
        engine, client, risk, store, exchange = stack
        assert engine.execute(buy_intent()).ok
        row = store.open_positions()[0]
        meta = _json.loads(row["meta"]) if isinstance(row["meta"], str) else row["meta"]
        exchange.orders[meta["stop_order_link_id"]]["orderStatus"] = "Cancelled"
        exchange.balances["BTC"] = 0.0           # sold/moved behind our back
        summary = engine.observe_exits()
        assert summary.get("stop_dead_unverified") == ["BTCUSDT"]
        assert "stop_dead" not in summary
        assert not store.is_kill_switch_engaged()[0]
        assert store.open_positions()           # left for reconcile


class TestTheCoinCheckCountsReservedCoin:
    def test_a_position_whose_coin_is_reserved_by_resting_legs_is_still_held(
            self, stack):
        import json as _json
        engine, client, risk, store, exchange = stack
        assert engine.execute(buy_intent()).ok
        row = store.open_positions()[0]
        meta = _json.loads(row["meta"]) if isinstance(row["meta"], str) else row["meta"]
        exchange.orders[meta["stop_order_link_id"]]["orderStatus"] = "Rejected"
        # the venue reports the coin as RESERVED: nothing free, all in the wallet
        real = client.get_wallet
        qty = float(row["qty"])

        def wallet(account_type="UNIFIED"):
            w = dict(real(account_type))
            w["coin"] = [{"coin": "BTC", "walletBalance": str(qty),
                          "availableToWithdraw": "0"}]
            return w
        client.get_wallet = wallet
        summary = engine.observe_exits()
        assert summary.get("stop_dead") == ["BTCUSDT"]
        assert "stop_dead_unverified" not in summary


class TestDeadStopFollowUps:
    def _dead(self, stack):
        import json as _json
        engine, client, risk, store, exchange = stack
        assert engine.execute(buy_intent()).ok
        row = store.open_positions()[0]
        meta = _json.loads(row["meta"]) if isinstance(row["meta"], str) else row["meta"]
        exchange.orders[meta["stop_order_link_id"]]["orderStatus"] = "Rejected"
        for tp in meta.get("tp_order_ids") or []:
            exchange.orders[tp]["orderStatus"] = "PartiallyFilled"
        return engine, client, store, exchange, meta

    def test_a_part_filled_leg_defers_the_reprotection_only_a_bounded_time(
            self, stack):
        engine, client, store, exchange, meta = self._dead(stack)
        assert meta.get("tp_order_ids"), "fixture must place take-profit legs"
        for _ in range(engine.DEAD_STOP_MAX_DEFERRALS):
            assert "stop_dead" not in engine.observe_exits()
        final = engine.observe_exits()
        assert final.get("stop_dead") == ["BTCUSDT"]

    def test_the_relaxed_path_frees_the_coin_the_resting_legs_reserve(self, stack):
        engine, client, store, exchange, meta = self._dead(stack)
        for _ in range(engine.DEAD_STOP_MAX_DEFERRALS + 1):
            engine.observe_exits()
        statuses = [exchange.orders[t]["orderStatus"]
                    for t in meta["tp_order_ids"]]
        assert all(st in ("Cancelled", "Filled") for st in statuses), statuses

    def test_a_stuck_leg_is_alarmed_once_and_never_sent_to_the_emergency_path(
            self, stack):
        engine, client, store, exchange, meta = self._dead(stack)
        client.cancel_order = lambda **kw: False        # the venue refuses
        outcomes = []
        for _ in range(2 * engine.DEAD_STOP_MAX_DEFERRALS + 3):
            summary = engine.observe_exits()
            outcomes.append(summary.get("stop_dead_outcome", {}).get("BTCUSDT"))
        # after repeated refusals the engine stops waiting and handles the
        # naked position itself: it must end re-protected or flattened, never
        # silently left on a dead stop
        # a leg that will not cancel never sends the book to the emergency
        # path (it would only trip the kill switch): it is alarmed once and
        # the cancel is retried
        assert not store.is_kill_switch_engaged()[0]
        assert store.open_positions()
        assert "BTCUSDT" in summary.get("stop_dead", [])
        stuck = [d for d in store.recent_decisions()
                 if d.get("decision") == "STOP_DEAD_LEG_STUCK"]
        assert len(stuck) == 1

    def test_the_two_cycle_sequence_books_the_partial_leg_then_reprotects(
            self, stack):
        engine, client, store, exchange, meta = self._dead(stack)
        tp = meta["tp_order_ids"][0]
        part = float(store.open_positions()[0]["qty"]) / 4
        exchange.orders[tp].update(cumExecQty=str(part), avgPrice=str(ENTRY * 1.02))
        for _ in range(engine.DEAD_STOP_MAX_DEFERRALS + 1):
            engine.observe_exits()                    # last one cancels the legs
        # the venue now reports the cancelled, part-filled leg as final
        exchange.orders[tp]["orderStatus"] = "PartiallyFilledCanceled"
        for other in meta["tp_order_ids"][1:]:
            exchange.orders[other]["orderStatus"] = "Cancelled"
        before = float(store.open_positions()[0]["qty"])
        engine.observe_exits()                        # books the partial fill
        rows = store.open_positions()
        assert rows and float(rows[0]["qty"]) == pytest.approx(before - part)
        assert len(store.recent_trades()) == 1        # the leg's P&L is booked
        engine.observe_exits()                        # stop re-judged
        assert not store.is_kill_switch_engaged()[0]
        import json as _j
        m = store.open_positions()[0]["meta"]
        m = _j.loads(m) if isinstance(m, str) else m
        # the booking replaced the dead stop with a live one on the true size
        assert m["stop_order_link_id"] != meta["stop_order_link_id"]
        assert exchange.orders[m["stop_order_link_id"]]["orderStatus"] in (
            "Untriggered", "New")

    def test_a_leg_cancelled_with_a_fill_is_booked_not_lost(self, stack):
        engine, client, store, exchange, meta = self._dead(stack)
        tp = meta["tp_order_ids"][0]
        part = float(store.open_positions()[0]["qty"]) / 4
        exchange.orders[tp].update(cumExecQty=str(part), avgPrice=str(ENTRY * 1.02),
                                   orderStatus="Cancelled")
        engine.observe_exits()
        assert len(store.recent_trades()) == 1

    def test_the_stuck_leg_retry_needs_no_balance_read_and_cancels_engine_ids(
            self, stack):
        engine, client, store, exchange, meta = self._dead(stack)
        cancelled = []
        real = client.cancel_order

        def spy(**kw):
            cancelled.append(kw["order_link_id"])
            return real(**kw)
        client.cancel_order = spy
        # the balance endpoint is broken AND the local order table forgets the legs
        client.get_coin_balance = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("wallet down"))
        engine._cancel_take_profit_legs = lambda symbol: True
        for _ in range(engine.DEAD_STOP_MAX_DEFERRALS + 1):
            engine.observe_exits()
        assert set(meta["tp_order_ids"]) <= set(cancelled)

    def test_a_stuck_leg_reports_the_symbol_once_per_cycle(self, stack):
        engine, client, store, exchange, meta = self._dead(stack)
        client.cancel_order = lambda **kw: False
        for _ in range(2 * engine.DEAD_STOP_MAX_DEFERRALS):
            engine.observe_exits()
        summary = engine.observe_exits()
        assert summary.get("stop_dead") == ["BTCUSDT"]

    def test_an_unpriceable_cancelled_fill_defers_instead_of_the_stale_size_path(
            self, stack):
        engine, client, store, exchange, meta = self._dead(stack)
        tp = meta["tp_order_ids"][0]
        exchange.orders[tp].update(orderStatus="Cancelled", cumExecQty="0.0001",
                                   avgPrice="0", cumExecValue="0", price="0")
        for other in meta["tp_order_ids"][1:]:
            exchange.orders[other]["orderStatus"] = "New"
        summary = engine.observe_exits()
        assert "stop_dead_outcome" not in summary      # no protect/flatten attempt
        assert not store.is_kill_switch_engaged()[0]

    def test_an_unpriceable_fill_is_booked_provisionally_not_at_the_entry_price(
            self, stack):
        engine, client, store, exchange, meta = self._dead(stack)
        tp = meta["tp_order_ids"][0]
        exchange.orders[tp].update(orderStatus="Cancelled", cumExecQty="0.0001",
                                   avgPrice="0", cumExecValue="0", price="0")
        for other in meta["tp_order_ids"][1:]:
            exchange.orders[other]["orderStatus"] = "New"
        for _ in range(engine.DEAD_STOP_MAX_DEFERRALS):
            engine.observe_exits()
        assert not store.provisional_trades()          # still deferring
        summary = engine.observe_exits()               # the bound is crossed
        assert summary.get("exits_provisional") == 1
        rows = store.provisional_trades()
        assert len(rows) == 1
        # never represented as a fill price, and not a confirmed trade
        assert rows[0]["exit_price"] == 0.0 and rows[0]["gross_pnl"] == 0.0
        assert rows[0]["price_provisional"] == 1
        assert not store.recent_trades()
        assert store.net_returns() == []
        assert store.fee_evidence()["price_provisional_trades"] == 1
        assert any(d.get("decision") == "EXIT_UNPRICED_PROVISIONAL"
                   for d in store.recent_decisions())

    def test_with_reserved_coin_the_cancel_first_path_still_reprotects(self, stack):
        engine, client, store, exchange, meta = self._dead(stack)
        exchange.reserve_limit_sells = True      # resting legs reserve their coin
        exchange.balances["BTC"] = float(store.open_positions()[0]["qty"])
        tp = meta["tp_order_ids"][0]
        part = float(store.open_positions()[0]["qty"]) / 4
        exchange.orders[tp].update(cumExecQty=str(part), avgPrice=str(ENTRY * 1.02))
        # sanity: nothing is free for a market sell while the legs rest
        assert exchange._free("BTC", exchange.balances["BTC"]) < float(
            store.open_positions()[0]["qty"])
        for _ in range(engine.DEAD_STOP_MAX_DEFERRALS + 1):
            engine.observe_exits()                # last one cancels the legs
        for t in meta["tp_order_ids"]:
            if exchange.orders[t]["orderStatus"] == "Cancelled" and t == tp:
                exchange.orders[t]["orderStatus"] = "PartiallyFilledCanceled"
        engine.observe_exits()                    # books the partial fill
        engine.observe_exits()
        assert not store.is_kill_switch_engaged()[0]
        assert store.open_positions()
        free = exchange._free("BTC", exchange.balances["BTC"])
        assert free >= float(store.open_positions()[0]["qty"]) - 1e-9

    def test_a_cancelled_unfilled_leg_is_not_read_every_cycle(self, stack):
        import json as _json
        engine, client, risk, store, exchange = stack
        assert engine.execute(buy_intent()).ok
        row = store.open_positions()[0]
        meta = _json.loads(row["meta"]) if isinstance(row["meta"], str) else row["meta"]
        tps = meta["tp_order_ids"]
        assert tps
        for t in tps:
            exchange.orders[t]["orderStatus"] = "Cancelled"
        reads = []
        real = client.get_order

        def spy(**kw):
            reads.append(kw["order_link_id"])
            return real(**kw)
        client.get_order = spy
        engine.observe_exits()
        engine.observe_exits()          # two dead reads confirm it
        first = [r for r in reads if r in tps]
        reads.clear()
        engine.observe_exits()
        engine.observe_exits()
        assert len(first) == 2 * len(tps)
        assert not [r for r in reads if r in tps]      # not read again

    def test_one_stale_dead_read_does_not_hide_a_fill_that_settles_after(self, stack):
        import json as _json
        engine, client, risk, store, exchange = stack
        assert engine.execute(buy_intent()).ok
        row = store.open_positions()[0]
        meta = _json.loads(row["meta"]) if isinstance(row["meta"], str) else row["meta"]
        tp = meta["tp_order_ids"][0]
        exchange.orders[tp]["orderStatus"] = "Cancelled"       # stale snapshot
        engine.observe_exits()
        part = float(store.open_positions()[0]["qty"]) / 4
        exchange.orders[tp].update(orderStatus="PartiallyFilledCanceled",
                                   cumExecQty=str(part), avgPrice=str(ENTRY * 1.02))
        engine.observe_exits()                                 # the fill books
        assert len(store.recent_trades()) == 1

    def test_the_grace_restarts_once_the_stop_is_healthy_again(self, stack):
        engine, client, store, exchange, meta = self._dead(stack)
        engine.observe_exits()
        assert engine._dead_stop_deferrals.get("BTCUSDT") == 1
        exchange.orders[meta["stop_order_link_id"]]["orderStatus"] = "Untriggered"
        engine.observe_exits()
        assert "BTCUSDT" not in engine._dead_stop_deferrals

    def test_total_balance_is_refused_when_the_row_does_not_state_it(self, stack):
        engine, client, risk, store, exchange = stack
        client.get_wallet = lambda account_type="UNIFIED": {
            "coin": [{"coin": "BTC", "availableToWithdraw": "1"}]}
        import bybit_connection as _bc
        with pytest.raises(_bc.PermanentAPIError):
            client.get_coin_balance("BTC", total=True)
        assert client.get_coin_balance("BTC") == 1.0


class TestARefusedEntryStartsTheSymbolCooldown:
    def test_the_same_refused_order_is_not_resent_next_tick(self, stack):
        engine, client, risk, store, exchange = stack
        risk.trade_cooldown_seconds = 60.0
        exchange.reject_next_with = (10001, "simulated rejection")
        first = engine.execute(buy_intent())
        assert first.ok is False and first.stage == "entry"
        creates = lambda: sum(  # noqa: E731
            1 for r in exchange.requests if r["url"].endswith("/order/create"))
        before = creates()
        second = engine.execute(buy_intent())
        assert second.ok is False
        assert second.reason == "SYMBOL_COOLDOWN" or "COOLDOWN" in str(
            getattr(second, "detail", "")) + str(second.reason)
        assert creates() == before


class TestNeverNaked:
    def test_a_rejected_stop_closes_the_position_and_trips_the_kill_switch(
        self, stack
    ):
        """Rule 9. The single most important behaviour in the codebase."""
        engine, client, risk, store, exchange = stack

        real_create = exchange._create_order

        def reject_stops(body):
            if body.get("orderFilter") == "StopOrder":
                return exchange._err(10001, "stop rejected by exchange")
            return real_create(body)

        exchange._create_order = reject_stops

        report = engine.execute(buy_intent())

        assert report.ok is False
        assert report.stage == "emergency"
        assert report.reason == "BRACKET_FAILED"
        assert store.is_kill_switch_engaged()[0] is True, "kill switch did not trip"
        assert store.open_positions() == [], "position left open after bracket failure"
        assert store.positions_without_stops() == []

    def test_kill_switch_blocks_all_subsequent_trades(self, stack):
        engine, client, risk, store, exchange = stack
        store.trip_kill_switch("manual test")
        report = engine.execute(buy_intent())
        assert report.ok is False
        assert report.reason == "KILL_SWITCH_ENGAGED"
        assert exchange.orders == {}

    def test_nothing_in_the_engine_can_clear_the_kill_switch(self, stack):
        engine, client, risk, store, exchange = stack
        store.trip_kill_switch("test")
        clearers = [
            n for n in dir(engine)
            if "clear" in n.lower() and ("kill" in n.lower() or "switch" in n.lower())
        ]
        assert clearers == []
        assert store.is_kill_switch_engaged()[0] is True

    def test_an_unverifiable_stop_is_treated_as_no_stop(self, stack):
        """A stop the exchange will not confirm does not count as protection."""
        engine, client, risk, store, exchange = stack

        real_query = exchange._query_order

        def hide_stops(params, path):
            status, text = real_query(params, path)
            payload = json.loads(text)
            payload["result"]["list"] = [
                r for r in payload["result"]["list"]
                if r.get("orderFilter") != "StopOrder"
            ]
            return status, json.dumps(payload)

        exchange._query_order = hide_stops

        report = engine.execute(buy_intent())
        assert report.stage == "emergency"
        assert store.is_kill_switch_engaged()[0] is True

    def test_an_incoherent_exit_plan_is_refused_before_entering(self, stack):
        engine, client, risk, store, exchange = stack
        report = engine.execute(buy_intent(stop_price=ENTRY * 1.02))  # stop above entry
        assert report.ok is False
        assert exchange.orders == {}, "an order was placed despite a bad exit plan"

    def test_take_profit_above_position_size_is_refused(self, stack):
        """Legs summing past 100% would sell inventory that is not held.

        Priced so the ladder's weighted average still clears the risk/reward
        gate, isolating the fraction check as the thing under test.
        """
        engine, client, risk, store, exchange = stack
        report = engine.execute(buy_intent(
            take_profits=((ENTRY * 1.024, 0.8), (ENTRY * 1.056, 0.8))
        ))
        assert report.ok is False
        assert report.reason.startswith("TP_FRACTIONS_EXCEED_POSITION")
        assert exchange.orders == {}, "an order was placed despite a bad ladder"

    def test_a_failed_take_profit_leg_does_not_abort_a_protected_position(
        self, stack
    ):
        """A missing TP costs upside. A missing stop costs capital. Only one is fatal."""
        engine, client, risk, store, exchange = stack

        real_create = exchange._create_order

        def reject_limits(body):
            if body.get("orderType") == "Limit":
                return exchange._err(10001, "tp rejected")
            return real_create(body)

        exchange._create_order = reject_limits

        report = engine.execute(buy_intent())
        assert report.ok is True
        assert report.take_profit_ids == ()
        assert report.stop_order_link_id
        assert store.positions_without_stops() == []


# ---------------------------------------------------------------------------
# no double execution
# ---------------------------------------------------------------------------


class TestNoDoubleExecution:
    def test_one_entry_order_per_attempt(self, stack):
        engine, client, risk, store, exchange = stack
        engine.execute(buy_intent())
        entries = exchange.submitted_orders("entry")
        assert len(entries) == 1

    def test_an_unconfirmed_fill_does_not_trigger_a_second_entry(self, stack):
        """The legacy fallback placed a second full-size market order here."""
        engine, client, risk, store, exchange = stack

        real_query = exchange._query_order

        def never_confirm(params, path):
            return exchange._ok({"list": []})

        exchange._query_order = never_confirm

        report = engine.execute(buy_intent())
        assert report.reason == "FILL_UNCONFIRMED"
        assert len(exchange.submitted_orders("entry")) == 1

    def test_a_blocked_gate_sends_no_orders_at_all(self, stack):
        engine, client, risk, store, exchange = stack
        report = engine.execute(buy_intent(confidence=0.10))   # below MIN_CONFIDENCE
        assert report.ok is False
        assert report.reason == "CONFIDENCE_TOO_LOW"
        assert exchange.orders == {}


class TestExecutedQuantityIsVenueConfirmed:
    """A nominally ``Filled`` order with no reported ``cumExecQty`` is a venue-
    response anomaly, not evidence of a full fill. Defaulting it to the
    REQUESTED quantity (as the entry path used to) would silently open a
    position sized from intent rather than from what the exchange actually
    confirms filled, and would make the ZERO_FILL guard below it unreachable.
    The exit path never had this bug: it already defaults the same field to
    0.0, which is what the entry path is made to match."""

    def test_missing_fill_qty_on_a_filled_order_is_zero_fill_not_the_requested_size(
        self, stack
    ):
        engine, client, risk, store, exchange = stack
        real_create = exchange._create_order

        def filled_but_qty_unreported(body):
            status, text = real_create(body)
            row = exchange.orders.get(body.get("orderLinkId", ""))
            if (row is not None and row.get("_purpose") == "entry"
                    and row.get("orderStatus") == "Filled"):
                row["cumExecQty"] = "0"
            return status, text

        exchange._create_order = filled_but_qty_unreported

        report = engine.execute(buy_intent())
        assert report.ok is False
        assert report.reason == "ZERO_FILL"
        assert store.open_positions() == []


# ---------------------------------------------------------------------------
# signal handling
# ---------------------------------------------------------------------------


class TestSignalHandling:
    def test_strong_buy_is_a_buy(self):
        """Audit C13: one site was patched, a second still mapped it to Sell."""
        assert te.TradeIntent(
            symbol="BTCUSDT", signal_type="STRONG_BUY",
            entry_price=1.0, stop_price=0.9,
        ).side == "Buy"

    def test_strong_sell_is_a_sell(self):
        assert te.TradeIntent(
            symbol="BTCUSDT", signal_type="STRONG_SELL",
            entry_price=1.0, stop_price=1.1,
        ).side == "Sell"

    @pytest.mark.parametrize("signal", ["HOLD", "NEUTRAL", "", "MAYBE", "sideways"])
    def test_non_directional_signals_do_not_become_trades(self, stack, signal):
        """HOLD is first-class. The legacy code mapped a score of 0 to BUY."""
        engine, client, risk, store, exchange = stack
        report = engine.execute(buy_intent(signal_type=signal))
        assert report.ok is False
        assert report.reason.startswith("NO_TRADEABLE_SIDE")
        assert exchange.orders == {}

    def test_exit_side_is_the_opposite_of_entry(self):
        long_intent = te.TradeIntent(
            symbol="X", signal_type="BUY", entry_price=1.0, stop_price=0.9
        )
        short_intent = te.TradeIntent(
            symbol="X", signal_type="SELL", entry_price=1.0, stop_price=1.1
        )
        assert long_intent.exit_side == "Sell"
        assert short_intent.exit_side == "Buy"


# ---------------------------------------------------------------------------
# stop management
# ---------------------------------------------------------------------------


class TestStopManagement:
    def test_stop_replacement_places_before_cancelling(self, stack):
        """Cancel-first leaves a window with no protection at all."""
        engine, client, risk, store, exchange = stack
        report = engine.execute(buy_intent())
        before = len(exchange.requests)

        engine.move_stop(
            symbol="BTCUSDT", exit_side="Sell", qty=report.qty,
            new_stop=ENTRY * 0.99, old_stop_link_id=report.stop_order_link_id,
        )

        after = exchange.requests[before:]
        create_at = next(
            i for i, r in enumerate(after) if r["url"].endswith("/order/create")
        )
        cancel_at = next(
            i for i, r in enumerate(after) if r["url"].endswith("/order/cancel")
        )
        assert create_at < cancel_at, "old stop was cancelled before the new one existed"

    def test_a_failed_replacement_keeps_the_original_stop(self, stack):
        engine, client, risk, store, exchange = stack
        report = engine.execute(buy_intent())

        real_create = exchange._create_order
        exchange._create_order = lambda body: exchange._err(10001, "nope")

        result = engine.move_stop(
            symbol="BTCUSDT", exit_side="Sell", qty=report.qty,
            new_stop=ENTRY * 0.99, old_stop_link_id=report.stop_order_link_id,
        )
        exchange._create_order = real_create

        assert result is None
        original = exchange.orders[report.stop_order_link_id]
        assert original["orderStatus"] == "Untriggered", "original stop was cancelled"

    def test_breakeven_ratchet_moves_the_stop_to_entry(self, stack):
        engine, client, risk, store, exchange = stack
        report = engine.execute(buy_intent())
        entry_price = report.detail["avg_price"]

        new_id = engine.move_stop_to_breakeven(
            symbol="BTCUSDT", exit_side="Sell", qty=report.qty,
            entry_price=entry_price, old_stop_link_id=report.stop_order_link_id,
        )
        assert new_id
        positions = {p["symbol"]: p for p in store.open_positions()}
        assert float(positions["BTCUSDT"]["stop_price"]) == pytest.approx(entry_price)


# ---------------------------------------------------------------------------
# closing and accounting
# ---------------------------------------------------------------------------


class TestClosing:
    def test_close_records_a_fee_inclusive_trade(self, stack):
        engine, client, risk, store, exchange = stack
        report = engine.execute(buy_intent())

        close = engine.close_position(
            symbol="BTCUSDT", reason="manual",
            entry_fee=1.0, exit_fee=1.0,
        )
        assert close.ok
        trades = store.recent_trades()
        assert len(trades) == 1
        assert trades[0]["entry_fee"] == 1.0
        assert trades[0]["exit_fee"] == 1.0
        assert trades[0]["net_pnl"] == pytest.approx(
            trades[0]["gross_pnl"] - 2.0
        )

    def test_a_losing_close_starts_the_symbol_cooldown(self, stack):
        engine, client, risk, store, exchange = stack
        risk.trade_cooldown_seconds = 60.0
        engine.execute(buy_intent())
        close = engine.close_position(
            symbol="BTCUSDT", reason="manual", entry_fee=50.0, exit_fee=50.0)
        assert close.ok
        assert store.consecutive_losses() >= 1
        assert store.symbol_cooldown_remaining("BTCUSDT") > 0.0

    def test_close_report_exposes_exit_identity_and_execution_evidence(self, stack):
        """FIX (exit assurance): close_position()'s report must carry the
        EXIT's own identity/quantity/price -- extending the existing
        ExecutionReport.detail path, not a parallel model -- so a caller
        can require real WS evidence keyed on the exit's own
        orderLinkId/orderId, exactly like it already can for entry."""
        engine, client, risk, store, exchange = stack
        engine.execute(buy_intent())

        close = engine.close_position(symbol="BTCUSDT", reason="manual")

        assert close.ok
        assert close.detail["exit_order_link_id"], "must be a real venue link id"
        assert close.detail["exit_order_id"], "must be a real venue order id"
        assert close.detail["exit_side"] == "Sell"
        assert close.detail["requested_exit_qty"] == close.qty
        # The fake fills market orders completely and immediately.
        assert close.detail["executed_exit_qty"] == pytest.approx(close.qty)
        assert close.detail["exit_status"] == "Filled"
        assert close.detail["exit_avg_price"] > 0

    def test_rejected_close_still_reports_whatever_exit_identity_exists(self, stack):
        engine, client, risk, store, exchange = stack
        engine.execute(buy_intent())
        exchange.reject_next_with = (10001, "simulated rejection")

        close = engine.close_position(symbol="BTCUSDT", reason="manual")

        assert close.ok is False
        assert close.detail["exit_status"] == "REJECTED"
        assert close.detail["exit_side"] == "Sell"
        assert "requested_exit_qty" in close.detail

    def test_close_cancels_resting_orders_only_after_confirmation(self, stack):
        engine, client, risk, store, exchange = stack
        engine.execute(buy_intent())
        engine.close_position(symbol="BTCUSDT", reason="manual")
        assert store.open_positions() == []
        assert all(
            o["orderStatus"] == "Cancelled"
            for o in exchange.orders.values()
            if o["_purpose"] in ("stop", "tp")
        )

    def test_closing_an_absent_position_is_refused(self, stack):
        engine, client, risk, store, exchange = stack
        result = engine.close_position(symbol="ETHUSDT", reason="manual")
        assert result.ok is False
        assert result.reason == "NO_SUCH_POSITION"


# ---------------------------------------------------------------------------
# chaos: restart mid-flight
# ---------------------------------------------------------------------------


class TestChaos:
    def test_restart_after_a_submitted_entry_does_not_duplicate_it(
        self, tmp_path, exchange
    ):
        """kill -9 between submit and ack, then restart."""
        cfg = LiveCfg()
        db = str(tmp_path / "state.db")

        store1 = StateStore(db)
        client1 = bc.BybitClient(config=cfg, store=store1, transport=exchange)
        result = client1.place_order(symbol="BTCUSDT", side="Buy", qty=0.01)
        assert result.ok
        store1.update_order_status(result.order_link_id, "pending")  # ack never arrived
        store1.close()

        store2 = StateStore(db)
        client2 = bc.BybitClient(config=cfg, store=store2, transport=exchange)
        summary = client2.reconcile_on_startup()

        assert summary["resolved"] == 1
        assert len(exchange.submitted_orders("entry")) == 1, "restart duplicated the order"
        store2.close()

    def test_restart_finds_and_protects_a_naked_position(self, tmp_path, exchange):
        cfg = LiveCfg()
        db = str(tmp_path / "state.db")

        store1 = StateStore(db)
        store1.update_equity(EQUITY)
        store1.upsert_position("BTCUSDT", "Buy", 0.01, ENTRY, stop_price=0.0)
        store1.close()

        store2 = StateStore(db)
        client = bc.BybitClient(config=cfg, store=store2, transport=exchange)
        risk = BillionaireRiskManager(config=cfg, store=store2)
        engine = te.TradingEngine(
            client=client, risk_manager=risk, store=store2, config=cfg
        )
        engine.reconcile()

        assert store2.positions_without_stops() == [], "naked position left unprotected"
        stops = exchange.submitted_orders("stop")
        assert len(stops) == 1
        store2.close()

    def test_exchange_returning_garbage_produces_no_trade(self, stack):
        engine, client, risk, store, exchange = stack
        exchange.scripted = [(200, "<html>maintenance</html>")] * 20
        report = engine.execute(buy_intent())
        assert report.ok is False
        assert exchange.orders == {}

    def test_zero_balance_produces_no_trade(self, stack):
        engine, client, risk, store, exchange = stack
        exchange.balances["USDT"] = 0.0
        report = engine.execute(buy_intent())
        assert report.ok is False
        assert exchange.orders == {}

    def test_shutdown_keeps_protective_stops(self, stack):
        engine, client, risk, store, exchange = stack
        report = engine.execute(buy_intent())
        engine.shutdown()
        stop = exchange.orders[report.stop_order_link_id]
        assert stop["orderStatus"] == "Untriggered", "shutdown cancelled the stop"

    def test_shutdown_cancels_working_take_profits(self, stack):
        engine, client, risk, store, exchange = stack
        report = engine.execute(buy_intent())
        for oid in report.take_profit_ids:
            store.update_order_status(oid, "pending")
        engine.shutdown()
        for oid in report.take_profit_ids:
            assert exchange.orders[oid]["orderStatus"] == "Cancelled"


# ---------------------------------------------------------------------------
# paper mode
# ---------------------------------------------------------------------------


class TestPaperMode:
    def test_paper_mode_sends_nothing_to_the_exchange(self, tmp_path, exchange):
        class PaperCfg(LiveCfg):
            PAPER_TRADING = True

        store = StateStore(str(tmp_path / "s.db"))
        store.update_equity(EQUITY)
        cfg = PaperCfg()
        client = bc.BybitClient(config=cfg, store=store, transport=exchange)
        risk = BillionaireRiskManager(config=cfg, store=store)
        engine = te.TradingEngine(
            client=client, risk_manager=risk, store=store, config=cfg
        )
        report = engine.execute(buy_intent())

        assert report.ok is True
        assert report.reason == "PAPER_OK"
        assert exchange.orders == {}, "paper mode reached the exchange"
        store.close()

    def test_paper_mode_still_runs_every_gate(self, tmp_path, exchange):
        class PaperCfg(LiveCfg):
            PAPER_TRADING = True

        store = StateStore(str(tmp_path / "s.db"))
        store.update_equity(EQUITY)
        cfg = PaperCfg()
        client = bc.BybitClient(config=cfg, store=store, transport=exchange)
        risk = BillionaireRiskManager(config=cfg, store=store)
        engine = te.TradingEngine(
            client=client, risk_manager=risk, store=store, config=cfg
        )
        report = engine.execute(buy_intent(confidence=0.05))
        assert report.ok is False
        assert report.reason == "CONFIDENCE_TOO_LOW"
        store.close()


class TestAnUnconfirmedCloseKeepsTheProtectiveOrders:
    def _watch_cancel_all(self, client):
        calls = []
        real = client.cancel_all

        def spy(symbol=None):
            calls.append(symbol)
            return real(symbol)
        client.cancel_all = spy
        return calls

    def test_a_close_whose_fill_never_resolves_does_not_cancel_the_stops(self, stack):
        """The docstring promises the stops come off AFTER the close is
        confirmed. An accepted close whose fill never resolved (timeout, or an
        order that ended Rejected/Cancelled) may not have reduced the position;
        cancelling its stops then left it open and unprotected."""
        engine, client, risk, store, exchange = stack
        engine.execute(buy_intent())
        cancels = self._watch_cancel_all(client)
        engine._await_fill = lambda symbol, link, timeout_s=10.0: None
        close = engine.close_position(symbol="BTCUSDT", reason="manual")
        assert cancels == [], "the protective orders were cancelled unconfirmed"
        assert close.ok                        # booked, marked unconfirmed
        events = store._query(
            "SELECT * FROM decisions WHERE decision = ?",
            ("CLOSE_UNCONFIRMED_STOPS_KEPT",))
        assert events, "the kept stops were not journaled"

    def test_a_confirmed_close_still_cancels_the_stops(self, stack):
        engine, client, risk, store, exchange = stack
        engine.execute(buy_intent())
        cancels = self._watch_cancel_all(client)
        close = engine.close_position(symbol="BTCUSDT", reason="manual")
        assert close.ok and cancels == ["BTCUSDT"]


class TestAnUnresolvedEntryBlocksAnotherEntry:
    def test_a_second_entry_is_refused_while_the_first_is_unresolved(self, stack):
        """An entry that was sent but never seen to fill may be a live position
        the book does not know. A 60s cooldown expiring must not let a second
        entry stack on it."""
        engine, client, risk, store, exchange = stack
        engine._await_fill = lambda symbol, link, timeout_s=10.0: None
        first = engine.execute(buy_intent())
        assert first.reason == "FILL_UNCONFIRMED"
        sent = len([r for r in exchange.requests if "order/create" in r["url"]])
        store._exec("DELETE FROM symbol_cooldown")             # the 60s ran out
        second = engine.execute(buy_intent())
        assert second.ok is False
        assert second.reason == "ENTRY_ORDER_UNRESOLVED"
        assert len([r for r in exchange.requests
                    if "order/create" in r["url"]]) == sent


def test_the_position_row_names_its_take_profit_orders(stack):
    """Spot exits are observed from the protective orders, so the row must
    carry the ids of the legs the entry placed."""
    import json
    engine, client, risk, store, exchange = stack
    report = engine.execute(buy_intent())
    assert report.ok
    meta = json.loads(store.open_positions()[0]["meta"])
    assert list(meta["tp_order_ids"]) == list(report.take_profit_ids)
    assert meta["tp_order_ids"], "no take-profit legs were placed to name"


def test_take_profit_legs_that_do_not_cover_the_position_keep_their_fractions(stack):
    """Legs summing to 0.7 must be 0.5Q and 0.2Q; the last leg takes the
    residual only when the legs are meant to cover the whole position."""
    engine, client, risk, store, exchange = stack
    placed = []
    real = client.place_take_profit

    def spy(**kw):
        placed.append(float(kw["qty"]))
        return real(**kw)
    client.place_take_profit = spy
    report = engine.execute(buy_intent(
        take_profits=((ENTRY * 1.04, 0.5), (ENTRY * 1.06, 0.2))))
    assert report.ok, report.reason
    assert len(placed) == 2
    assert placed[1] / placed[0] == pytest.approx(0.4, rel=0.05)


class TestAStopThatTriggeredWhileTheProcessWasDown:
    """Restart reconciliation of a spot protective stop, no venue contact."""

    def _restart(self, stack):
        engine, client, risk, store, exchange = stack
        path = store.path          # the old process is simply gone: reopen
        cfg = LiveCfg()
        store2 = StateStore(path)
        store2.claim_writer()
        client2 = bc.BybitClient(config=cfg, store=store2, transport=exchange)
        risk2 = BillionaireRiskManager(config=cfg, store=store2)
        engine2 = te.TradingEngine(
            client=client2, risk_manager=risk2,
            position_sizer=BillionairePositionSizing(config=cfg, risk_manager=risk2),
            store=store2, config=cfg)
        return engine2, client2, store2

    def _meta(self, store):
        import json as _j
        row = store.open_positions()[0]
        m = row["meta"]
        return row, (_j.loads(m) if isinstance(m, str) else m)

    def test_a_full_stop_fill_is_booked_once_after_the_restart(self, stack):
        engine, client, risk, store, exchange = stack
        assert engine.execute(buy_intent()).ok
        row, meta = self._meta(store)
        qty = float(row["qty"])
        stop = meta["stop_order_link_id"]
        exchange.orders[stop].update(orderStatus="Filled", cumExecQty=str(qty),
                                     avgPrice=str(ENTRY * 0.98))
        engine2, client2, store2 = self._restart(stack)
        engine2.observe_exits()
        engine2.observe_exits()                       # replay: still once
        assert store2.open_positions() == []
        trades = store2.recent_trades()
        assert len(trades) == 1
        assert trades[0]["exit_price"] == pytest.approx(ENTRY * 0.98)
        assert trades[0]["gross_pnl"] < 0
        for tp in meta["tp_order_ids"]:               # the OCO half
            assert exchange.orders[tp]["orderStatus"] == "Cancelled"

    def test_a_partial_stop_fill_then_a_restart_books_the_part_and_keeps_the_rest(
            self, stack):
        engine, client, risk, store, exchange = stack
        assert engine.execute(buy_intent()).ok
        row, meta = self._meta(store)
        qty = float(row["qty"])
        stop = meta["stop_order_link_id"]
        part = qty / 4
        exchange.orders[stop].update(orderStatus="PartiallyFilledCanceled",
                                     cumExecQty=str(part),
                                     avgPrice=str(ENTRY * 0.98))
        engine2, client2, store2 = self._restart(stack)
        engine2.observe_exits()
        rows = store2.open_positions()
        assert len(rows) == 1 and float(rows[0]["qty"]) == pytest.approx(qty - part)
        assert len(store2.recent_trades()) == 1
        engine2.observe_exits()
        assert len(store2.recent_trades()) == 1       # not booked twice
