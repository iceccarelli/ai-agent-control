"""Funding settled by the venue against a directional linear position is booked
from the account's own records -- once, signed, with an explicit status."""
from __future__ import annotations

import os
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import bybit_connection as bc  # noqa: E402
import directional_funding as df  # noqa: E402
from carry_broker import CarryBroker  # noqa: E402
from fake_bybit import FakeBybit  # noqa: E402
from persistence import StateStore, TradeRecord  # noqa: E402
from test_orchestrator import Cfg  # noqa: E402


class LinearCfg(Cfg):
    CATEGORY = "linear"
    ALLOW_SHORTS = True


NOW = int(time.time() * 1000)
H = 3600 * 1000


def _setup(tmp_path):
    fake = FakeBybit()
    store = StateStore(str(tmp_path / "s.db"))
    store.claim_writer()
    store.update_equity(10_000.0)
    client = bc.BybitClient(config=LinearCfg(), store=store, transport=fake)
    broker = CarryBroker(client=client, sequence_source=lambda *a: 0,
                         order_gate=lambda *a, **k: (False, "read-only"))
    return fake, store, broker


def _trade(store, *, opened_h_ago=30.0, closed_h_ago=8.0, side="Sell",
           symbol="BTCUSDT", provisional=False, net=0.0):
    return store.record_trade(TradeRecord(
        symbol=symbol, side=side, qty=0.01, entry_price=60_000.0,
        exit_price=0.0 if provisional else 59_000.0,
        price_provisional=provisional,
        gross_pnl=0.0 if provisional else 10.0, entry_fee=0.1, exit_fee=0.1,
        opened_epoch=(NOW - opened_h_ago * H) / 1000.0,
        closed_epoch=(NOW - closed_h_ago * H) / 1000.0,
        meta={"funding_booked": False}))


def _row(i, usd, hours_ago, symbol="BTCUSDT", size="-0.01"):
    return {"id": f"S{i}", "symbol": symbol, "type": "SETTLEMENT",
            "category": "linear", "funding": str(usd), "change": str(usd),
            "transactionTime": str(int(NOW - hours_ago * H)),
            "currency": "USDT", "size": size}


def _meta(store, tid):
    import json
    return json.loads(store._query("SELECT meta FROM trades WHERE id=?", (tid,))[0]["meta"])


def _net(store, tid):
    return float(store._query("SELECT net_pnl FROM trades WHERE id=?", (tid,))[0]["net_pnl"])


def _run(store, broker, **kw):
    return df.reconcile_directional_funding(store, broker, now_ms=NOW, **kw)


def test_received_and_paid_funding_are_booked_with_their_sign(tmp_path):
    fake, store, broker = _setup(tmp_path)
    tid = _trade(store)
    base = _net(store, tid)
    fake.settlements += [_row(1, 0.30, 12), _row(2, -0.05, 10)]
    r = _run(store, broker)
    assert r["booked"] == 1
    assert _net(store, tid) == pytest.approx(base + 0.25)
    m = _meta(store, tid)
    assert m["funding_booked"] is True and m["funding_usd"] == pytest.approx(0.25)
    assert m["funding_status"] == "BOOKED_FROM_SETTLEMENTS"
    assert m["funding_payment_ids"] == ["S1", "S2"]
    assert store.fee_evidence()["funding_unbooked_trades"] == 0


def test_a_replay_books_nothing_twice(tmp_path):
    fake, store, broker = _setup(tmp_path)
    tid = _trade(store)
    fake.settlements.append(_row(1, 0.30, 12))
    _run(store, broker)
    net = _net(store, tid)
    assert _run(store, broker) == {"booked": 0, "no_settlement": 0, "left": 0,
                                   "statuses": {}}
    assert _net(store, tid) == pytest.approx(net)


def test_a_short_hold_with_a_complete_empty_log_is_zero_not_missing(tmp_path):
    fake, store, broker = _setup(tmp_path)
    tid = _trade(store, opened_h_ago=8.5, closed_h_ago=8.0)
    r = _run(store, broker)
    assert r["no_settlement"] == 1
    assert _meta(store, tid)["funding_status"] == "NO_SETTLEMENT_IN_HOLD_WINDOW"


def test_a_long_hold_with_no_settlement_is_suspect_and_stays_unbooked(tmp_path):
    fake, store, broker = _setup(tmp_path)
    tid = _trade(store)
    r = _run(store, broker)
    assert r["statuses"] == {"SUSPECT_NO_SETTLEMENT_OVER_INTERVAL": 1}
    assert _meta(store, tid)["funding_booked"] is False
    assert store.fee_evidence()["funding_unbooked_trades"] == 1


def test_an_unreadable_log_leaves_the_trade_unbooked(tmp_path):
    fake, store, broker = _setup(tmp_path)
    tid = _trade(store)
    fake.transaction_log_error = 10016
    r = _run(store, broker)
    assert r["booked"] == 0 and r["left"] == 1
    m = _meta(store, tid)
    assert m["funding_booked"] is False
    assert m["funding_status"] in ("EVIDENCE_UNREAD", "EVIDENCE_INCOMPLETE")
    # and a later healthy read books it
    fake.transaction_log_error = None
    fake.settlements.append(_row(1, 0.2, 10))
    assert _run(store, broker)["booked"] == 1


def test_a_malformed_row_makes_the_evidence_incomplete(tmp_path):
    fake, store, broker = _setup(tmp_path)
    tid = _trade(store)
    bad = _row(1, 0.2, 10)
    bad["funding"] = "nan"
    fake.settlements += [bad, _row(2, 0.2, 9)]
    r = _run(store, broker)
    assert r["booked"] == 0
    assert _meta(store, tid)["funding_status"] == "EVIDENCE_INCOMPLETE"


def test_a_recent_exit_waits_for_late_settlement_rows(tmp_path):
    fake, store, broker = _setup(tmp_path)
    tid = _trade(store, opened_h_ago=3.0, closed_h_ago=0.05)
    r = _run(store, broker)
    assert r["statuses"] == {"PENDING_SETTLEMENT_GRACE": 1}
    assert _meta(store, tid)["funding_booked"] is False


def test_rows_for_other_symbols_and_outside_the_hold_are_not_attributed(tmp_path):
    fake, store, broker = _setup(tmp_path)
    tid = _trade(store, opened_h_ago=40.0, closed_h_ago=8.0)
    fake.settlements += [_row(1, 9.0, 45), _row(2, 9.0, 7),
                         _row(3, 9.0, 20, symbol="ETHUSDT"), _row(4, 0.4, 30)]
    _run(store, broker)
    assert _meta(store, tid)["funding_usd"] == pytest.approx(0.4)


def test_a_settlement_belongs_to_the_exit_row_open_when_it_settled(tmp_path):
    fake, store, broker = _setup(tmp_path)
    a = _trade(store, opened_h_ago=40.0, closed_h_ago=20.0)
    b = _trade(store, opened_h_ago=40.0, closed_h_ago=8.0)
    fake.settlements += [_row(1, 0.5, 30, size="-0.02"), _row(2, 0.3, 12)]
    _run(store, broker)
    assert _meta(store, a)["funding_usd"] == pytest.approx(0.5)
    assert _meta(store, b)["funding_usd"] == pytest.approx(0.3)


def test_the_same_settlement_id_with_two_amounts_is_not_booked(tmp_path):
    fake, store, broker = _setup(tmp_path)
    tid = _trade(store)
    r1, r2 = _row(1, 0.3, 12), _row(1, 0.9, 12)
    r2["transactionTime"] = str(int(r1["transactionTime"]) + 1)
    fake.settlements += [r1, r2]
    _run(store, broker)
    assert _meta(store, tid)["funding_booked"] is False


def test_a_symbol_the_carry_book_also_trades_is_not_attributed(tmp_path):
    fake, store, broker = _setup(tmp_path)
    tid = _trade(store)
    fake.settlements.append(_row(1, 0.3, 12))
    r = _run(store, broker, carry_symbols={"BTCUSDT"})
    assert r["statuses"] == {"AMBIGUOUS_SHARED_WITH_CARRY": 1}
    assert _meta(store, tid)["funding_booked"] is False


def test_funding_booked_before_a_provisional_exit_is_priced_survives_it(tmp_path):
    fake, store, broker = _setup(tmp_path)
    tid = _trade(store, provisional=True)
    fake.settlements.append(_row(1, 0.5, 12))
    _run(store, broker)
    assert _net(store, tid) == pytest.approx(-0.2 + 0.5)
    assert store.reconcile_provisional_trade(
        tid, exit_price=59_000.0, gross_pnl=10.0, exit_fee=0.1,
        exit_fee_realized=True, exit_fee_currency="USDT", evidence={})
    assert _net(store, tid) == pytest.approx(10.0 - 0.1 - 0.1 + 0.5)


def test_the_day_realised_pnl_moves_with_the_booked_funding(tmp_path):
    fake, store, broker = _setup(tmp_path)
    tid = _trade(store)
    day = store._query("SELECT day FROM trades WHERE id=?", (tid,))[0]["day"]
    before = store._query("SELECT realised_pnl FROM daily_anchor WHERE day=?", (day,))
    fake.settlements.append(_row(1, -0.7, 12))
    _run(store, broker)
    after = store._query("SELECT realised_pnl FROM daily_anchor WHERE day=?", (day,))
    assert before, "the day's anchor row must exist for the check to mean anything"
    assert float(after[0]["realised_pnl"]) == pytest.approx(
        float(before[0]["realised_pnl"]) - 0.7)


def test_no_order_is_ever_sent(tmp_path):
    fake, store, broker = _setup(tmp_path)
    _trade(store)
    fake.settlements.append(_row(1, 0.3, 12))
    _run(store, broker)
    assert fake.submitted_orders() == []


class TestTheLoopHook:
    """`TradingBot._book_directional_funding` is what the tick loop calls."""

    def _bot(self, store, client, carry=None):
        import types
        import main as app
        bot = types.SimpleNamespace(
            engine=types.SimpleNamespace(client=client), store=store, carry=carry,
            DIRECTIONAL_FUNDING_POLL_MS=app.TradingBot.DIRECTIONAL_FUNDING_POLL_MS,
            _last_directional_funding_ms=0)
        return app.TradingBot._book_directional_funding, bot

    def test_it_books_for_a_linear_client_and_is_throttled(self, tmp_path):
        fake, store, broker = _setup(tmp_path)
        client = broker.client
        client.is_linear = True
        tid = _trade(store)
        fake.settlements.append(_row(1, 0.3, 12))
        run, bot = self._bot(store, client)
        run(bot)
        assert _meta(store, tid)["funding_booked"] is True
        assert bot._last_directional_funding_ms > 0
        tid2 = _trade(store, opened_h_ago=40.0, closed_h_ago=30.0)
        fake.settlements.append(_row(2, 0.1, 35))
        run(bot)                                   # inside the poll interval
        assert _meta(store, tid2)["funding_booked"] is False
        bot._last_directional_funding_ms = 0
        run(bot)
        assert _meta(store, tid2)["funding_booked"] is True

    def test_it_does_nothing_for_a_spot_client(self, tmp_path):
        fake, store, broker = _setup(tmp_path)
        client = broker.client
        client.is_linear = False
        tid = _trade(store)
        fake.settlements.append(_row(1, 0.3, 12))
        run, bot = self._bot(store, client)
        run(bot)
        assert _meta(store, tid)["funding_booked"] is False


def test_a_pile_of_stuck_rows_cannot_starve_the_trades_behind_them(tmp_path):
    fake, store, broker = _setup(tmp_path)
    for _ in range(3):                       # old, unresolvable (carry symbol)
        _trade(store, opened_h_ago=30.0, closed_h_ago=20.0, symbol="ETHUSDT")
    df.reconcile_directional_funding(store, broker, now_ms=NOW, limit=3,
                                     carry_symbols={"ETHUSDT"})
    fresh = _trade(store, opened_h_ago=8.5, closed_h_ago=8.0)
    r = df.reconcile_directional_funding(store, broker, now_ms=NOW, limit=3,
                                         carry_symbols={"ETHUSDT"})
    assert _meta(store, fresh)["funding_booked"] is True
    assert r["no_settlement"] == 1


class TestAttributionIsSafeNotJustSigned:
    def test_a_recent_exit_is_not_final_before_the_ledgers_grace(self, tmp_path):
        fake, store, broker = _setup(tmp_path)
        tid = _trade(store, opened_h_ago=10.0, closed_h_ago=5.0)   # < 6 h ago
        fake.settlements.append(_row(1, 0.3, 7))
        r = _run(store, broker)
        assert r["statuses"] == {"PENDING_SETTLEMENT_GRACE": 1}
        assert _meta(store, tid)["funding_booked"] is False

    def test_a_row_whose_size_is_not_what_the_trade_held_is_not_booked(self, tmp_path):
        fake, store, broker = _setup(tmp_path)
        tid = _trade(store)
        fake.settlements.append(_row(1, 0.3, 12, size="-0.5"))
        assert _run(store, broker)["statuses"] == {"SETTLEMENT_SIZE_MISMATCH": 1}
        assert _meta(store, tid)["funding_booked"] is False

    def test_a_row_with_no_size_is_not_booked(self, tmp_path):
        fake, store, broker = _setup(tmp_path)
        tid = _trade(store)
        row = _row(1, 0.3, 12)
        del row["size"]
        fake.settlements.append(row)
        assert _run(store, broker)["statuses"] == {"SETTLEMENT_SIZE_UNVERIFIED": 1}

    @pytest.mark.parametrize("hours", [30 - 0.005, 8 + 0.005])
    def test_a_settlement_within_a_minute_of_either_end_is_ambiguous(
            self, tmp_path, hours):
        fake, store, broker = _setup(tmp_path)
        tid = _trade(store)
        fake.settlements.append(_row(1, 0.3, hours))
        assert _run(store, broker)["statuses"] == {"BOUNDARY_AMBIGUOUS": 1}
        assert _meta(store, tid)["funding_booked"] is False

    def test_two_positions_alive_at_the_same_time_are_not_given_a_row(self, tmp_path):
        fake, store, broker = _setup(tmp_path)
        a = _trade(store, opened_h_ago=30.0, closed_h_ago=8.0)
        b = _trade(store, opened_h_ago=9.0, closed_h_ago=7.5)      # opened before a "closed"
        fake.settlements.append(_row(1, 0.3, 12))
        r = _run(store, broker)
        assert r["booked"] == 0 and r["no_settlement"] == 0
        assert _meta(store, a)["funding_status"] == "AMBIGUOUS_OVERLAPPING_POSITION"
        assert _meta(store, b)["funding_status"] == "AMBIGUOUS_OVERLAPPING_POSITION"

    def test_a_hold_of_an_hour_or_more_with_an_empty_log_is_never_zero(self, tmp_path):
        fake, store, broker = _setup(tmp_path)
        tid = _trade(store, opened_h_ago=10.0, closed_h_ago=8.0)    # 2 h hold
        r = _run(store, broker)
        assert r["statuses"] == {"SUSPECT_NO_SETTLEMENT_OVER_INTERVAL": 1}

    def test_an_id_booked_elsewhere_with_another_amount_is_a_conflict(self, tmp_path):
        fake, store, broker = _setup(tmp_path)
        a = _trade(store, opened_h_ago=60.0, closed_h_ago=40.0)
        fake.settlements.append(_row(1, 1.0, 50))
        assert _run(store, broker)["booked"] == 1
        b = _trade(store, opened_h_ago=30.0, closed_h_ago=8.0)
        fake.settlements.append(_row(1, 2.0, 12))        # same id, other amount
        r = _run(store, broker)
        assert r["statuses"] == {"DUPLICATE_ID_CONFLICT": 1}
        assert _meta(store, b)["funding_booked"] is False

    def test_a_perpetual_without_its_funding_is_not_a_confirmed_sample(self, tmp_path):
        fake, store, broker = _setup(tmp_path)
        tid = _trade(store)
        assert store.net_returns() == []                  # fees-only: no sample
        assert store.recent_trades() == []
        assert store.fee_evidence()["realized_net_pnl"] == 0.0
        fake.settlements.append(_row(1, 0.3, 12))
        _run(store, broker)
        assert len(store.net_returns()) == 1 and len(store.recent_trades()) == 1

    def test_one_unbooked_piece_keeps_the_whole_position_out(self, tmp_path):
        fake, store, broker = _setup(tmp_path)
        _trade(store, opened_h_ago=40.0, closed_h_ago=20.0)
        b = _trade(store, opened_h_ago=40.0, closed_h_ago=8.0)
        bad = _row(2, 0.3, 12)
        bad["change"] = "-0.3"                            # piece 2: sign mismatch
        fake.settlements += [_row(1, 0.5, 30, size="-0.02"), bad]
        r = _run(store, broker)
        assert r["booked"] == 1 and _meta(store, b)["funding_booked"] is False
        assert store.net_returns() == []                  # not a sample yet
