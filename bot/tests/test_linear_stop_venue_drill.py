"""linear_stop_venue_drill.py: the venue evidence Part 1 Item 1 actually
needs — BybitClient.place_stop_order + verify_stop against a real LINEAR
account, never the carry book's own drill.

Two layers, matching the repo's own convention for this exact mechanism
(tests/test_linear_simulator.py::TestTheProtectiveStopEndToEnd):
  - a FakeClient drives the full nine-stage sequence with controllable
    misbehaviour (this is where every fail-closed branch is proven);
  - one test drives `attach`/`verify` against the REAL BybitClient +
    LinearSimulatedExchange, so the drill's stage logic is proven against
    the actual simulated venue mechanics, not only against a hand-rolled
    fake that could quietly drift from what BybitClient really does.
"""
from __future__ import annotations

import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import linear_stop_venue_drill as D  # noqa: E402
from bybit_connection import BybitClient, OrderResult  # noqa: E402
from persistence import StateStore  # noqa: E402
from position_sizing import InstrumentFilters  # noqa: E402

SYMBOL = "BTCUSDT"
MARK = 100_000.0


def _cfg(**over):
    base = {"BYBIT_VENUE": "testnet", "PAPER_TRADING": False, "CATEGORY": "linear"}
    base.update(over)
    return types.SimpleNamespace(**base)


class FakeClient:
    """A venue that can be made to misbehave on command."""

    def __init__(self, *, mark=MARK, venue_perp=0.0, wallet_ok=True,
                 open_ok=True, attach_ok=True, verify_live=True,
                 flatten_ok=True, category="linear", venue="testnet",
                 cfg=None):
        self.mark = mark
        self.venue_perp = venue_perp
        self.wallet_ok = wallet_ok
        self.open_ok = open_ok
        self.attach_ok = attach_ok
        self.verify_live = verify_live
        self.flatten_ok = flatten_ok
        self.category = category
        self.venue = venue
        self.cfg = cfg if cfg is not None else _cfg(CATEGORY=category)
        self.orders = []
        self.position_size = 0.0
        self.stop_loss = 0.0

    def get_last_price(self, symbol):
        return self.mark

    def get_wallet(self):
        if not self.wallet_ok:
            raise RuntimeError("wallet unreadable")
        return {"accountType": "UNIFIED"}

    def get_position(self, symbol):
        size = self.position_size if self.position_size else self.venue_perp
        if size <= 0:
            return None
        return {"symbol": symbol, "side": "Buy", "size": str(size),
                "avgPrice": str(self.mark), "stopLoss": str(self.stop_loss),
                "tpslMode": "Full", "slTriggerBy": "LastPrice",
                "slOrderType": "Market", "positionIdx": 0}

    def get_instrument_filters(self, symbol, force=False):
        return InstrumentFilters(symbol=symbol, tick_size="0.1",
                                 qty_step="0.001", min_qty="0.001",
                                 min_notional="5", from_exchange=True)

    def place_order(self, *, symbol, side, qty, order_type="Market",
                    purpose="", filters=None, reduce_only=False, **kw):
        self.orders.append((side, qty, purpose, reduce_only))
        if reduce_only:
            if not self.flatten_ok:
                return OrderResult(reason="FLATTEN_REJECTED")
            self.position_size = 0.0
            self.stop_loss = 0.0
            return OrderResult(ok=True, order_link_id="flat-1")
        if not self.open_ok:
            return OrderResult(reason="OPEN_REJECTED")
        self.position_size = float(qty)
        return OrderResult(ok=True, order_link_id="open-1")

    def place_stop_order(self, *, symbol, side, qty, trigger_price,
                         filters=None, order_link_id=None):
        if not self.attach_ok:
            return OrderResult(reason="STOP_REJECTED")
        self.stop_loss = trigger_price
        return OrderResult(ok=True, order_link_id="stop-1",
                           raw={"mechanism": "position"})

    def verify_stop(self, *, symbol, order_link_id):
        if self.verify_live is False:
            # Forced failure, for the fail-closed branch - independent of
            # actual stop_loss state.
            return False, "POSITION_HAS_NO_STOP_LOSS"
        live = self.stop_loss > 0
        return live, (f"stopLoss={self.stop_loss}" if live
                      else "POSITION_HAS_NO_STOP_LOSS")


def run(client, **kw):
    kw.setdefault("arm", True)
    kw.setdefault("notional", 100.0)
    return D.run_drill(client=client, **kw)


class TestThePreflightAlonePassesWithoutArming:
    def test_a_dry_run_sends_nothing(self):
        client = FakeClient()
        report = run(client, arm=False)
        assert report["armed"] is False
        assert report["orders_sent"] == 0
        assert client.orders == []
        assert report["verdict"] == "PREFLIGHT_ONLY"

    def test_a_dry_run_reports_reachability_and_auth(self):
        report = run(FakeClient(), arm=False)
        ev = report["evidence"]
        assert ev["reachability"]["last_price"] == MARK
        assert ev["auth"]["auth_ok"] is True
        assert ev["flat"]["venue_perp_qty"] == 0.0


class TestItRefusesBeforeItRisksAnything:
    def test_an_unreachable_venue_stops_at_preflight(self):
        class Blind(FakeClient):
            def get_last_price(self, symbol):
                raise RuntimeError("venue unreachable")
        report = run(Blind(), arm=False)
        assert report["verdict"] == "FAILED"
        assert report["failed_stage"] == "reachability"

    def test_unreadable_wallet_fails_auth(self):
        report = run(FakeClient(wallet_ok=False), arm=False)
        assert report["verdict"] == "FAILED"
        assert report["failed_stage"] == "auth"

    def test_a_position_already_open_stops_it(self):
        report = run(FakeClient(venue_perp=0.5), arm=False)
        assert report["verdict"] == "FAILED"
        assert report["failed_stage"] == "flat"
        assert report["orders_sent"] == 0

    @pytest.mark.parametrize("bad_cfg", [
        _cfg(BYBIT_VENUE="mainnet"),
        _cfg(PAPER_TRADING=True),
        _cfg(CATEGORY="spot"),
    ])
    def test_arming_refuses_off_the_one_safe_configuration(self, bad_cfg):
        client = FakeClient(cfg=bad_cfg)
        with pytest.raises(D.LinearStopDrillRefused):
            D._assert_can_arm(client.cfg)
        # run_drill itself must refuse too, not just the CLI's own check.
        report = run(client)
        assert report["verdict"] == "FAILED"
        assert report["failed_stage"] == "arm_gate"
        assert report["orders_sent"] == 0


class TestTheHappyPathIsAWholeRoundTrip:
    def test_it_opens_attaches_verifies_flattens_and_reconciles(self):
        client = FakeClient()
        report = run(client)
        assert report["verdict"] == "PASSED", report.get("failed_stage")
        assert report["orders_sent"] == 2
        assert report["evidence"]["verify"]["live"] is True
        assert report["evidence"]["final_reconcile"]["verdict"] == "FLAT"
        assert report["evidence"]["final_reconcile"]["stop_still_live"] is False

    def test_position_evidence_dumps_the_real_stop_fields(self):
        report = run(FakeClient())
        ev = report["evidence"]["position_evidence"]
        assert ev["stopLoss"] not in (None, "0", "0.0", "")
        assert ev["tpslMode"] == "Full"


class TestVerifyIsFailClosed:
    def test_a_live_false_verify_fails_the_drill_not_a_silent_pass(self):
        report = run(FakeClient(verify_live=False))
        assert report["verdict"] == "FAILED"
        assert report["failed_stage"] == "verify"
        assert report["evidence"]["verify"]["live"] is False

    def test_the_stop_call_being_rejected_also_fails(self):
        report = run(FakeClient(attach_ok=False))
        assert report["verdict"] == "FAILED"
        assert report["failed_stage"] == "attach"

    def test_the_open_leg_being_rejected_stops_before_any_stop_is_attempted(self):
        client = FakeClient(open_ok=False)
        report = run(client)
        assert report["verdict"] == "FAILED"
        assert report["failed_stage"] == "open"
        assert client.stop_loss == 0.0


class TestAFlattenThatDoesNotLandIsLoud:
    def test_still_open_is_reported_and_a_human_is_told_to_act(self):
        report = run(FakeClient(flatten_ok=False))
        assert report["verdict"] == "FAILED"
        assert report["failed_stage"] == "flatten"
        assert report["still_open"] is True
        assert "HUMAN MUST ACT" in report["next_action"]


class TestItNeverForgesTheGate:
    def test_the_module_never_touches_allows_live_or_the_checklist(self):
        with open(os.path.join(os.path.dirname(__file__), "..", "tools",
                               "linear_stop_venue_drill.py"), encoding="utf-8") as fh:
            body = fh.read()
        for banned in ("allows_live =", "allows_live=True", "\"complete\": True",
                       "clear_kill_switch", "HUMAN_CLEARED_KILL_SWITCH",
                       "promote_forward_shadow"):
            assert banned not in body


class TestLoadClientReadsTheRealEnvironment:
    """The live bug: `config.load({})` treats a non-None mapping as the
    ONLY env source, so `{}` means zero env vars regardless of what the
    shell actually exports - BYBIT_API_SECRET always empty ("cannot sign:
    no API secret configured") and CATEGORY always "spot" even with
    CATEGORY=linear exported. `_load_client` must call `config.load()`
    with no argument (or `None`), which reads the real `os.environ`."""

    def test_load_client_never_calls_load_with_an_empty_mapping(self):
        import ast
        path = os.path.join(os.path.dirname(__file__), "..", "tools",
                            "linear_stop_venue_drill.py")
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)
        target = next(n for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef) and n.name == "_load_client")
        for node in ast.walk(target):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "load"):
                assert not node.args, (
                    "_load_client must call config.load() with no argument, "
                    "not an empty mapping - {} silently discards os.environ")

    def test_load_client_sees_the_real_environment(self, monkeypatch):
        monkeypatch.setenv("BYBIT_VENUE", "testnet")
        monkeypatch.setenv("PAPER_TRADING", "0")
        monkeypatch.setenv("CATEGORY", "linear")
        monkeypatch.setenv("BYBIT_API_KEY", "k" * 18)
        monkeypatch.setenv("BYBIT_API_SECRET", "s" * 36)

        client, cfg = D._load_client()

        assert cfg.BYBIT_API_SECRET == "s" * 36
        assert cfg.CATEGORY == "linear"
        assert client.category == "linear"
        assert client.is_linear is True
        assert client.api_secret == "s" * 36


# ---------------------------------------------------------------------------
# against the real BybitClient + the real linear simulator - not only a fake
# ---------------------------------------------------------------------------


class TestAgainstTheRealSimulatedVenue:
    """Same pattern as test_linear_simulator.py::TestTheProtectiveStopEndToEnd:
    a real BybitClient talking to LinearSimulatedExchange, so `attach` and
    `verify` are proven against the actual simulated venue mechanics this
    tool's stage functions assume, not only a hand-written fake's promises."""

    @pytest.fixture()
    def client(self):
        import backtest as bt
        view = bt._backtest_config_view(bt.BacktestConfig(category="linear"))
        store = StateStore(":memory:")
        ex = bt.LinearSimulatedExchange(
            {SYMBOL: [bt.Bar(start_ms=1_600_000_000_000, open=100.0,
                             close=100.0, high=100.5, low=99.5, volume=100.0)]
             for _ in [0]}, starting_cash=1_000.0)
        ex.positions[SYMBOL] = bt._SimPosition(
            symbol=SYMBOL, side="Buy", size=1.0, entry_price=100.0)
        yield BybitClient(config=view, store=store, transport=ex), ex
        store.close()

    def test_attach_then_verify_reads_back_live(self, client):
        api, ex = client
        result = api.place_stop_order(
            symbol=SYMBOL, side="Sell", qty=1.0, trigger_price=95.0)
        assert result.ok, result.reason
        live, detail = api.verify_stop(
            symbol=SYMBOL, order_link_id=result.order_link_id)
        assert live is True, detail
        assert ex.positions[SYMBOL].stop_loss == pytest.approx(95.0)

    def test_a_stop_cleared_at_the_venue_reads_back_as_not_live(self, client):
        """The read is against the venue, not local state - same guarantee
        the drill's `verify` stage relies on."""
        api, ex = client
        result = api.place_stop_order(
            symbol=SYMBOL, side="Sell", qty=1.0, trigger_price=95.0)
        ex.positions[SYMBOL].stop_loss = 0.0
        live, detail = api.verify_stop(
            symbol=SYMBOL, order_link_id=result.order_link_id)
        assert live is False
        assert detail == "POSITION_HAS_NO_STOP_LOSS"
