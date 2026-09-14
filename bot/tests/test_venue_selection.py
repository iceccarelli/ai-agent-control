"""0046 — three venues, one truth about which one you are pointed at.

WHY
===
Phase D needs a venue the book can actually reach and actually trade on, and
the two human blockers were a testnet API key and testnet BTC. Testnet coins
come from a web faucet — 1 BTC, once per 24 hours, PC browser only — so the
wallet is a person's job.

Bybit's DEMO TRADING service is a different thing: `api-demo.bybit.com`, funded
by `POST /v5/account/demo-apply-money` (up to 15 BTC), and running against REAL
mainnet market data with simulated matching. So the marks, the basis and the
funding prints are the real ones, and the wallet is one API call.

That is worth having. What it is NOT is a second way to reach mainnet.

THE DANGER
==========
`USE_TESTNET` is a boolean that picks a base URL. Adding a third destination
to a boolean is how a testnet key ends up authenticating against mainnet.

So `BYBIT_VENUE` is explicit and three-way, `USE_TESTNET` becomes the DERIVED
sandbox flag every existing gate already reads, and a configuration that states
both and contradicts itself is REFUSED rather than resolved. Two sources of
truth about which exchange you are pointed at is exactly the bug this file
exists to make impossible.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import config as _config  # noqa: E402
from bybit_connection import DEMO_REST, MAINNET_REST, TESTNET_REST  # noqa: E402

BASE = {"BOOK_MODE": "carry", "CARRY_BORROW_APR": "0.0",
        "CARRY_EXECUTION_MODE": "overlay"}


def load(**env):
    return _config.load({**BASE, **env})


class TestTheDefaultDoesNotMove:
    def test_nothing_set_is_still_testnet(self):
        cfg = load()
        assert cfg.BYBIT_VENUE == "testnet"
        assert cfg.USE_TESTNET is True

    def test_use_testnet_0_is_still_mainnet(self):
        cfg = load(USE_TESTNET="0")
        assert cfg.BYBIT_VENUE == "mainnet"
        assert cfg.USE_TESTNET is False

    def test_use_testnet_1_is_still_testnet(self):
        cfg = load(USE_TESTNET="1")
        assert cfg.BYBIT_VENUE == "testnet"


class TestDemoIsASandboxEverywhere:
    def test_demo_derives_the_sandbox_flag(self):
        """Every existing gate reads USE_TESTNET. Demo must set it, or demo
        becomes a hole straight through the arming chain."""
        cfg = load(BYBIT_VENUE="demo")
        assert cfg.BYBIT_VENUE == "demo"
        assert cfg.USE_TESTNET is True

    def test_demo_is_not_live_authorised(self):
        armed, _why = _config.is_live_authorized(
            load(BYBIT_VENUE="demo", PAPER_TRADING="0",
                 LIVE_TRADING_ACK="I_UNDERSTAND", BYBIT_API_KEY="k",
                 BYBIT_API_SECRET="s"))
        assert armed is False

    def test_demo_may_start(self):
        may, _why = _config.assert_sandbox(load(BYBIT_VENUE="demo"))
        assert may is True

    def test_the_carry_gate_permits_demo_and_names_it(self):
        """A demo order is permitted for the same reason a testnet one is —
        it cannot touch real money — and the reason string must SAY demo, so
        no log or transcript can be mistaken for mainnet evidence."""
        import main as _main
        from unittest import mock
        cfg = load(BYBIT_VENUE="demo", PAPER_TRADING="0")
        bot = _main.build_bot(config=cfg, store=_Store(), client=mock.Mock(),
                              risk_manager=mock.Mock(), engine=mock.Mock())
        allowed, why = bot.carry.broker.inner.order_gate()
        assert allowed is True
        assert "DEMO" in why.upper()


class TestAContradictionIsRefused:
    @pytest.mark.parametrize("venue,testnet", [
        ("mainnet", "1"), ("testnet", "0"), ("demo", "0")])
    def test_two_sources_of_truth_are_refused(self, venue, testnet):
        with pytest.raises(_config.ConfigError) as err:
            load(BYBIT_VENUE=venue, USE_TESTNET=testnet)
        assert "USE_TESTNET" in str(err.value)

    @pytest.mark.parametrize("venue,testnet", [
        ("mainnet", "0"), ("testnet", "1"), ("demo", "1")])
    def test_agreement_is_accepted(self, venue, testnet):
        assert load(BYBIT_VENUE=venue, USE_TESTNET=testnet).BYBIT_VENUE == venue

    def test_an_unknown_venue_is_refused_not_defaulted(self):
        with pytest.raises(_config.ConfigError):
            load(BYBIT_VENUE="prod")


class TestTheUrlFollowsTheVenue:
    @pytest.mark.parametrize("venue,url", [
        ("mainnet", MAINNET_REST), ("testnet", TESTNET_REST),
        ("demo", DEMO_REST)])
    def test_the_client_points_where_the_config_says(self, venue, url):
        from bybit_connection import BybitClient
        cfg = load(BYBIT_VENUE=venue,
                   USE_TESTNET="0" if venue == "mainnet" else "1")
        client = BybitClient(config=cfg)
        assert client.base_url == url

    def test_the_three_urls_are_different(self):
        assert len({MAINNET_REST, TESTNET_REST, DEMO_REST}) == 3


class TestDemoFundingCannotTouchAnythingReal:
    class Client:
        def __init__(self, base):
            self.base_url = base
            self.sent = []

        def _request(self, method, endpoint, *, params=None, body=None,
                     signed=False, retries=3):
            self.sent.append((endpoint, body))
            return {"result": "ok"}

    def test_it_funds_a_demo_account(self):
        import fund_demo
        client = self.Client(DEMO_REST)
        fund_demo.apply(client, [("BTC", "1")])
        endpoint, body = client.sent[0]
        assert endpoint == "/v5/account/demo-apply-money"
        assert body["adjustType"] == 0
        assert body["utaDemoApplyMoney"] == [{"coin": "BTC",
                                              "amountStr": "1"}]

    @pytest.mark.parametrize("base", [MAINNET_REST, TESTNET_REST])
    def test_it_refuses_any_other_venue(self, base):
        import fund_demo
        client = self.Client(base)
        with pytest.raises(fund_demo.NotDemo):
            fund_demo.apply(client, [("BTC", "1")])
        assert client.sent == []

    def test_it_refuses_an_amount_above_the_venue_maximum(self):
        import fund_demo
        client = self.Client(DEMO_REST)
        with pytest.raises(ValueError):
            fund_demo.apply(client, [("BTC", "99")])
        assert client.sent == []

    def test_it_refuses_a_coin_the_venue_does_not_fund(self):
        import fund_demo
        client = self.Client(DEMO_REST)
        with pytest.raises(ValueError):
            fund_demo.apply(client, [("DOGE", "1")])


class TestTheDrillSaysWhereItRan:
    def test_a_transcript_names_the_venue(self):
        import sys as _sys
        _sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..",
                                         "tools"))
        import drill as D
        from test_drill import Venue
        report = D.run_drill(broker=Venue(), notional=100.0, arm=False,
                             venue="demo")
        assert report["venue"] == "demo"
        assert "demo" in report["venue_note"].lower()

    def test_an_unnamed_venue_is_recorded_as_unknown_not_mainnet(self):
        import sys as _sys
        _sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..",
                                         "tools"))
        import drill as D
        from test_drill import Venue
        report = D.run_drill(broker=Venue(), notional=100.0, arm=False)
        assert report["venue"] == "unknown"


class TestAMissingDependencyIsAMessageNotATraceback:
    def test_the_drill_says_which_interpreter_to_use(self, capsys):
        """Run with a bare python3 the drill died with `ModuleNotFoundError:
        No module named 'numpy'` from four imports deep. That is D16 again: a
        tool whose first failure is a traceback about a transitive dependency
        is a tool that gets run wrong at 2am."""
        import sys as _sys
        _sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..",
                                         "tools"))
        import drill as D
        from unittest import mock

        def boom():
            raise ModuleNotFoundError("No module named 'numpy'")

        with mock.patch.object(D, "_load_bot", boom):
            code = D.main([])
        assert code == 2
        out = capsys.readouterr()
        assert ".venv" in (out.out + out.err)
        assert "numpy" in (out.out + out.err)


class _Store:
    def __init__(self):
        self.seq = 0

    def next_order_seq(self):
        self.seq += 1
        return self.seq

    def trip_kill_switch(self, reason):
        pass

    def is_kill_switch_engaged(self):
        return (False, "")

    def save_carry_position(self, state):
        pass

    def load_carry_position(self):
        return None

    def save_ledger(self, rows):
        self.ledger_rows = list(rows)

    def load_ledger(self):
        return getattr(self, "ledger_rows", None)

    def open_positions(self):
        return []
