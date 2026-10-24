"""`tools/close_testnet_short.py`: close a stray testnet short, reduce-only,
and only after a typed confirmation. Fake brokers; no venue is touched."""
from __future__ import annotations

import io
import os
import sys
import types

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))
sys.path.insert(0, HERE)

import close_testnet_short as C  # noqa: E402
import drill as D  # noqa: E402
from bybit_connection import TESTNET_REST  # noqa: E402
from test_carry_broker import FakeClient, broker as make_broker  # noqa: E402


class FakeBroker:
    def __init__(self, short=0.001, orders=0, closes=True, stuck_reads=0,
                 slot_idx=0, reject=False):
        self.short, self.orders, self.closes = short, orders, closes
        self.stuck_reads = stuck_reads
        self.slot_idx, self.reject = slot_idx, reject
        self.sent = []
        self.sent_order_links = []
        self.client = types.SimpleNamespace(base_url=TESTNET_REST)

    def get_position_slots(self, symbol):
        size = self.get_perp_position(symbol)
        return [{"positionIdx": self.slot_idx, "side": "Sell", "size": size}] \
            if size else []

    def get_perp_position(self, symbol):
        if isinstance(self.short, Exception):
            raise self.short
        if self.sent and self.closes and self.stuck_reads <= 0:
            return 0.0
        if self.sent:
            self.stuck_reads -= 1
        return self.short

    def get_open_carry_orders(self, spot, perp):
        return [{}] * self.orders

    def place_market(self, **kw):
        self.sent.append(kw)
        self.sent_order_links.append(("linear", kw["symbol"], "L1"))
        if self.reject:
            return None
        return {"filled_qty": kw["qty"], "avg_price": 80000.0,
                "order_link_id": "L1"}


NO_SLEEP = lambda s: None  # noqa: E731


class TestCloseShort:
    def test_a_short_gets_exactly_one_reduce_only_buy_of_its_size(self):
        b = FakeBroker(short=0.001)
        out = C.close_short(b, "BTCUSDT", sleep=NO_SLEEP)
        assert b.sent == [{"symbol": "BTCUSDT", "side": "Buy", "qty": 0.001,
                           "product": "linear"}]
        assert out["ok"] is True and out["flat_verified"] is True

    def test_a_flat_venue_sends_nothing(self):
        b = FakeBroker(short=0.0)
        out = C.close_short(b, "BTCUSDT", sleep=NO_SLEEP)
        assert b.sent == [] and out["ok"] is True and out["sent"] is False

    def test_open_orders_refuse_and_send_nothing(self):
        b = FakeBroker(orders=1)
        out = C.close_short(b, "BTCUSDT", sleep=NO_SLEEP)
        assert b.sent == [] and out["ok"] is False and "open" in out["refused"]

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), -0.001])
    def test_an_unusable_size_is_refused(self, bad):
        b = FakeBroker(short=bad)
        out = C.close_short(b, "BTCUSDT", sleep=NO_SLEEP)
        assert b.sent == [] and out["ok"] is False

    def test_an_unreadable_or_non_short_position_raises_before_any_order(self):
        b = FakeBroker(short=RuntimeError("holds 0.001 on side Buy, not a short"))
        with pytest.raises(RuntimeError):
            C.close_short(b, "BTCUSDT", sleep=NO_SLEEP)
        assert b.sent == []

    def test_a_close_that_does_not_flatten_is_not_ok(self):
        b = FakeBroker(closes=False)
        out = C.close_short(b, "BTCUSDT", sleep=NO_SLEEP)
        assert out["sent"] is True and out["ok"] is False
        assert len(b.sent) == 1                  # never re-sent

    def test_a_lagging_position_read_is_retried_then_accepted(self):
        b = FakeBroker(stuck_reads=2)
        out = C.close_short(b, "BTCUSDT", sleep=NO_SLEEP)
        assert out["ok"] is True and len(b.sent) == 1

    def test_the_order_is_never_re_sent_whatever_the_reads_say(self):
        b = FakeBroker(closes=False)
        C.close_short(b, "BTCUSDT", sleep=NO_SLEEP)
        assert len(b.sent) == 1


class TestOnTheWire:
    """Through the real CarryBroker: the venue itself is told reduce-only."""

    def test_the_create_request_is_a_reduce_only_linear_market_buy(self):
        client = FakeClient(perp_qty=0.001)
        broker = make_broker(client)
        # FakeClient's position row lacks a side; give it the one Bybit sends
        real = client._request

        def request(method, endpoint, **kw):
            out = real(method, endpoint, **kw)
            if endpoint == "/v5/order/realtime" and "openOnly" in (kw.get("params") or {}):
                return {"retCode": 0, "retMsg": "OK", "result": {"list": []}}
            if endpoint == "/v5/position/list":
                for row in out["result"]["list"]:
                    row.setdefault("side", "Sell" if float(row["size"]) else "")
                    row.setdefault("positionIdx", 0)
            return out
        client._request = request
        out = C.close_short(broker, "BTCUSDT", sleep=NO_SLEEP)
        (body,) = client.created
        assert body["category"] == "linear" and body["side"] == "Buy"
        assert body["orderType"] == "Market" and body["reduceOnly"] is True
        assert body["qty"] == "0.001"
        assert out["ok"] is True


def bot(broker, **cfg_over):
    cfg = dict(BYBIT_VENUE="testnet", USE_TESTNET=True, PAPER_TRADING=False,
               LIVE_TRADING_ACK="", STATE_DB_PATH="")
    cfg.update(cfg_over)
    return types.SimpleNamespace(cfg=types.SimpleNamespace(**cfg),
                                 carry=types.SimpleNamespace(broker=broker))


@pytest.fixture()
def wired(monkeypatch):
    monkeypatch.setattr(D.InterruptGuard, "install", lambda self: None)
    monkeypatch.setattr(C.time, "sleep", lambda s: None)

    def install(b, **over):
        monkeypatch.setattr(D, "_load_bot", lambda p="": bot(b, **over))
    return install


class TestMain:
    def test_unarmed_reads_and_sends_nothing(self, wired, capsys):
        b = FakeBroker()
        wired(b)
        assert C.main([]) == 0
        assert b.sent == []
        out = capsys.readouterr().out
        assert "Nothing was sent" in out and "0.001" in out

    def test_a_flat_venue_says_so(self, wired, capsys):
        wired(FakeBroker(short=0.0))
        assert C.main(["--arm"]) == 0
        assert "Nothing to close" in capsys.readouterr().out

    @pytest.mark.parametrize("over", [{"BYBIT_VENUE": "mainnet"},
                                      {"USE_TESTNET": False},
                                      {"PAPER_TRADING": True},
                                      {"LIVE_TRADING_ACK": "yes"}])
    def test_anything_but_unarmed_testnet_is_refused(self, wired, over):
        b = FakeBroker()
        wired(b, **over)
        # unarmed on purpose: with --arm a missing terminal also returns 3, which
        # would pass this test whether or not the testnet refusal existed
        assert C.main([]) == 3 and b.sent == []

    def test_open_orders_are_refused(self, wired):
        b = FakeBroker(orders=2)
        wired(b)
        assert C.main(["--arm"]) == 3 and b.sent == []

    def test_a_position_that_is_not_a_plain_short_is_refused(self, wired):
        b = FakeBroker(short=RuntimeError("not a short"))
        wired(b)
        assert C.main(["--arm"]) == 3 and b.sent == []

    def test_arm_without_a_terminal_is_refused(self, wired, monkeypatch):
        b = FakeBroker()
        wired(b)
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        assert C.main(["--arm"]) == 3 and b.sent == []

    def test_a_wrong_confirmation_sends_nothing(self, wired, monkeypatch):
        b = FakeBroker()
        wired(b)
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
        monkeypatch.setattr("builtins.input", lambda prompt="": "yes")
        assert C.main(["--arm"]) == 3 and b.sent == []

    def test_the_right_confirmation_closes_and_verifies(self, wired, monkeypatch,
                                                       capsys):
        b = FakeBroker()
        wired(b)
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
        phrase = C.confirmation_phrase("BTCUSDT", 0.001)
        assert phrase == "CLOSE 0.00100000 BTCUSDT TESTNET"
        monkeypatch.setattr("builtins.input", lambda prompt="": phrase)
        assert C.main(["--arm"]) == 0
        assert len(b.sent) == 1 and b.sent[0]["side"] == "Buy"
        assert "FLAT VERIFIED" in capsys.readouterr().out

    def test_a_close_that_leaves_it_open_exits_1(self, wired, monkeypatch, capsys):
        b = FakeBroker(closes=False)
        wired(b)
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
        monkeypatch.setattr("builtins.input",
                            lambda prompt="": C.confirmation_phrase("BTCUSDT", 0.001))
        assert C.main(["--arm"]) == 1
        assert "NOT VERIFIED FLAT" in capsys.readouterr().err

    def test_an_existing_evidence_file_is_refused_before_anything_loads(
            self, tmp_path, monkeypatch):
        f = tmp_path / "e.jsonl"
        f.write_text("x")
        monkeypatch.setattr(D, "_load_bot", lambda *a: pytest.fail("loaded"))
        assert C.main(["--evidence-path", str(f)]) == 3

    def test_a_stack_that_will_not_start_exits_2(self, monkeypatch):
        def boom(p=""):
            raise RuntimeError("no credentials")
        monkeypatch.setattr(D, "_load_bot", boom)
        assert C.main([]) == 2

    def test_a_non_carry_book_exits_2(self, monkeypatch):
        b = bot(FakeBroker())
        b.carry = None
        monkeypatch.setattr(D, "_load_bot", lambda p="": b)
        assert C.main([]) == 2

    def test_an_interrupt_still_ends_with_a_verified_read(self, wired, monkeypatch):
        class Interrupted(FakeBroker):
            def place_market(self, **kw):
                super().place_market(**kw)
                raise KeyboardInterrupt("signal 15")
        b = Interrupted()
        wired(b)
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
        monkeypatch.setattr("builtins.input",
                            lambda prompt="": C.confirmation_phrase("BTCUSDT", 0.001))
        code = C.main(["--arm"])
        assert len(b.sent) == 1
        assert code == 0          # the venue read after the interrupt was flat

    def test_only_the_one_broker_order_path_is_used(self):
        import ast
        tree = ast.parse(open(C.__file__, encoding="utf-8").read())
        names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        assert names & {"place_market"} == {"place_market"}
        assert not names & {"place_post_only", "cancel_order", "_request",
                            "trip_kill_switch", "_unwind"}


# ---------------------------------------------------------------------------
# found by independent review
# ---------------------------------------------------------------------------

class SizeChanges(FakeBroker):
    """The short is 0.001 when read for the confirmation and 0.005 after."""

    def __init__(self):
        super().__init__(short=0.001)
        self.reads = 0

    def get_perp_position(self, symbol):
        self.reads += 1
        return 0.001 if self.reads <= 1 else 0.005


class TestReviewFindings:
    def test_the_quantity_sent_is_the_one_confirmed(self, wired, monkeypatch):
        b = SizeChanges()
        wired(b)
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
        monkeypatch.setattr("builtins.input",
                            lambda prompt="": C.confirmation_phrase("BTCUSDT", 0.001))
        assert C.main(["--arm"]) == 1
        assert b.sent == []                      # nothing at all went out

    def test_close_short_refuses_a_size_other_than_expected(self):
        out = C.close_short(FakeBroker(short=0.005), "BTCUSDT",
                            expected_qty=0.001, sleep=NO_SLEEP)
        assert out["ok"] is False and "changed while waiting" in out["refused"]

    @pytest.mark.parametrize("idx", [1, 2, "1", "2"])
    def test_a_hedge_mode_slot_is_refused(self, wired, idx):
        # unarmed on purpose: with --arm a missing terminal also returns 3
        b = FakeBroker(slot_idx=idx)
        wired(b)
        assert C.main([]) == 3 and b.sent == []

    @pytest.mark.parametrize("idx", [0, "0", None])
    def test_the_one_way_slot_is_accepted(self, wired, idx):
        wired(FakeBroker(slot_idx=idx))
        assert C.main([]) == 0

    def test_a_missing_client_is_a_refusal_not_a_skipped_check(self, wired):
        b = FakeBroker()
        del b.client
        wired(b)
        assert C.main([]) == 3 and b.sent == []

    def test_a_client_on_another_host_is_refused(self, wired):
        b = FakeBroker()
        b.client = types.SimpleNamespace(base_url="https://api.bybit.com")
        wired(b)
        assert C.main([]) == 3 and b.sent == []

    def test_a_wrapper_chain_that_never_reaches_the_adapter_is_refused(
            self, monkeypatch):
        class Wrap:
            def __init__(self, inner):
                self.inner = inner
        b = FakeBroker()
        for _ in range(9):
            b = Wrap(b)
        monkeypatch.setattr(D, "_load_bot", lambda p="": bot(b))
        assert C.main([]) == 3
        with pytest.raises(ValueError):
            D.unwrap_broker(b)

    def test_end_of_input_at_the_prompt_is_a_refusal(self, wired, monkeypatch):
        b = FakeBroker()
        wired(b)
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)

        def eof(prompt=""):
            raise EOFError
        monkeypatch.setattr("builtins.input", eof)
        assert C.main(["--arm"]) == 3 and b.sent == []

    def test_ctrl_c_at_the_prompt_is_a_refusal(self, wired, monkeypatch):
        b = FakeBroker()
        wired(b)
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)

        def interrupt(prompt=""):
            raise KeyboardInterrupt
        monkeypatch.setattr("builtins.input", interrupt)
        assert C.main(["--arm"]) == 3 and b.sent == []

    def test_a_signal_during_the_final_read_ends_in_the_look_at_the_venue_message(
            self, wired, monkeypatch, capsys):
        class SignalOnFinalRead(FakeBroker):
            def get_perp_position(self, symbol):
                if self.sent and getattr(self, "armed", False):
                    self.armed = False
                    raise KeyboardInterrupt("signal 15")
                if self.sent:
                    self.armed = True
                return super().get_perp_position(symbol)
        b = SignalOnFinalRead(closes=False)
        wired(b)
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
        monkeypatch.setattr("builtins.input",
                            lambda prompt="": C.confirmation_phrase("BTCUSDT", 0.001))
        code = C.main(["--arm"])             # no traceback escapes
        assert code == 1 and len(b.sent) == 1
        assert "NOT VERIFIED FLAT" in capsys.readouterr().err

    def test_a_definite_rejection_is_not_reported_as_sent(self):
        b = FakeBroker(reject=True)
        out = C.close_short(b, "BTCUSDT", expected_qty=0.001, sleep=NO_SLEEP)
        assert out["ok"] is False and out["sent"] is False
        assert "rejected" in out["note"] and len(b.sent) == 1


class TestPositionSlots:
    def test_the_adapter_reports_only_non_empty_rows_with_their_index(self):
        client = FakeClient(perp_qty=0.001)
        real = client._request

        def request(method, endpoint, **kw):
            out = real(method, endpoint, **kw)
            if endpoint == "/v5/position/list":
                out["result"]["list"] = [
                    {"symbol": "BTCUSDT", "size": "0.001", "side": "Sell",
                     "positionIdx": 2},
                    {"symbol": "BTCUSDT", "size": "0", "side": "", "positionIdx": 1}]
            return out
        client._request = request
        assert make_broker(client).get_position_slots("BTCUSDT") == [
            {"positionIdx": 2, "side": "Sell", "size": 0.001}]

    def test_an_unreadable_answer_raises_not_flat(self):
        client = FakeClient()
        real = client._request
        client._request = lambda m, e, **kw: (
            {"retCode": 0, "result": {}} if e == "/v5/position/list"
            else real(m, e, **kw))
        with pytest.raises(Exception, match="cannot read"):
            make_broker(client).get_position_slots("BTCUSDT")
