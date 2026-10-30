"""The pair is not opened unless the account can fund it.

The pre-trade gate checked the kill switch, freshness, a cap and a margin RATIO
that is undefined while flat; it never asked whether the account had the
capital. In acquire mode the venue found out at the perp leg, after the spot
leg had been bought, and the pair unwound for a round trip of fees. Now, with
the check on (build_bot turns it on), the engine reads the account's available
balance before any leg and refuses if it cannot cover the spot leg (acquire
only) plus the perp's initial margin at the configured leverage. An unreadable
or unusable balance refuses too. Real engine, brokers and journal; only the
venue is scripted. Offline.
"""
from __future__ import annotations

import os
import sys
from unittest import mock

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from carry_broker import CarryBroker, PairIncident          # noqa: E402
from carry_engine import BookState, CarryEngine             # noqa: E402
from test_carry_broker import FakeClient, envelope          # noqa: E402
from test_carry_fee_truth import (                          # noqa: E402
    H8, MARK, build, modern, open_the_hedge)


def with_available(venue, available):
    """Answer the account-level wallet read with `available` (None = absent,
    an Exception = the read fails)."""
    real = venue._request

    def request(method, endpoint, **kw):
        params = kw.get("params") or {}
        if endpoint == "/v5/account/wallet-balance" and "coin" not in params:
            if isinstance(available, Exception):
                raise available
            row = {"accountMMRate": "0.05"}
            if available is not None:
                row["totalAvailableBalance"] = available
            return envelope({"list": [row]})
        return real(method, endpoint, **kw)
    venue._request = request


def checked(available, **engine_kw):
    venue, journal, engine = build(modern)
    with_available(venue, available)
    engine.require_capital_check = True
    for k, v in engine_kw.items():
        setattr(engine, k, v)
    return venue, journal, engine


# -- the broker's read ------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [("250000.5", 250000.5), (1000, 1000.0),
                                          ("0", 0.0)])
def test_the_broker_reads_the_stated_available_balance(raw, expected):
    client = FakeClient()
    with_available(client, raw)
    b = CarryBroker(client=client, order_gate=lambda: (True, "T"),
                    sequence_source=lambda *a: 1)
    assert b.get_available_margin_usd() == pytest.approx(expected)


@pytest.mark.parametrize("bad", [None, "", "nan", "inf", "-1", "abc", True])
def test_an_unstated_or_unusable_balance_raises_not_zero(bad):
    client = FakeClient()
    with_available(client, bad)
    b = CarryBroker(client=client, order_gate=lambda: (True, "T"),
                    sequence_source=lambda *a: 1)
    with pytest.raises(PairIncident):
        b.get_available_margin_usd()


# -- the engine's refusal ------------------------------------------------------------

def test_an_account_that_cannot_fund_the_pair_is_refused_before_any_leg():
    venue, journal, engine = checked("150000")          # needs 200,000 at 1x
    d = open_the_hedge(engine)
    assert d.action == "stand_aside" and d.reason == "INSUFFICIENT_AVAILABLE_MARGIN"
    assert d.detail["required_usd"] == pytest.approx(200_000.0)
    assert d.detail["available_usd"] == pytest.approx(150_000.0)
    assert venue.created == [] and engine.state is BookState.FLAT


def test_an_account_that_can_fund_it_opens():
    venue, journal, engine = checked("250000")
    assert open_the_hedge(engine).action == "opened"


def test_the_requirement_follows_the_configured_leverage():
    venue, journal, engine = checked("150000", leverage=5.0)   # 100k spot + 20k
    assert open_the_hedge(engine).action == "opened"
    venue2, _j, engine2 = checked("110000", leverage=5.0)
    d = open_the_hedge(engine2)
    assert d.reason == "INSUFFICIENT_AVAILABLE_MARGIN"
    assert d.detail["required_usd"] == pytest.approx(120_000.0)


def test_overlay_needs_only_the_perp_margin():
    engine = CarryEngine(broker=mock.Mock(), max_notional_usd=100_000.0,
                         borrow_apr=0.0, execution_mode="overlay",
                         persist=lambda s: None, leverage=2.0,
                         require_capital_check=True)
    assert engine.required_capital_usd(100_000.0) == pytest.approx(50_000.0)
    acquire = CarryEngine(broker=mock.Mock(), max_notional_usd=100_000.0,
                          borrow_apr=0.05, execution_mode="acquire",
                          persist=lambda s: None, leverage=2.0,
                          require_capital_check=True)
    assert acquire.required_capital_usd(100_000.0) == pytest.approx(150_000.0)


@pytest.mark.parametrize("bad", [None, "", "nan", "-5", "abc"])
def test_an_unreadable_balance_refuses_and_places_nothing(bad):
    venue, journal, engine = checked(bad)
    d = open_the_hedge(engine)
    assert d.action == "stand_aside" and d.reason == "AVAILABLE_MARGIN_UNREADABLE"
    assert venue.created == []


def test_a_failing_read_refuses_and_places_nothing():
    venue, journal, engine = checked(ConnectionError("down"))
    d = open_the_hedge(engine)
    assert d.reason == "AVAILABLE_MARGIN_UNREADABLE" and venue.created == []


def test_a_broker_that_cannot_state_the_balance_refuses():
    venue, journal, engine = checked("250000")
    engine.broker = mock.Mock(spec=[])        # no get_available_margin_usd at all
    d = engine._capital_refusal(100_000.0)
    assert d is not None and d.reason == "AVAILABLE_MARGIN_UNREADABLE"


def test_the_check_is_off_unless_asked_for():
    venue, journal, engine = build(modern)
    with_available(venue, "1")
    assert engine.require_capital_check is False
    assert open_the_hedge(engine).action == "opened"


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf"), "x", None])
def test_a_nonsense_leverage_is_refused_at_construction(bad):
    with pytest.raises(ValueError):
        CarryEngine(broker=mock.Mock(), max_notional_usd=100.0, borrow_apr=0.0,
                    execution_mode="overlay", persist=lambda s: None,
                    leverage=bad, require_capital_check=True)


def test_build_bot_turns_the_check_on_with_the_configured_leverage():
    import config as _config
    import main as _main
    from test_carry_wiring_authority import _Store
    cfg = _config.load({"BOOK_MODE": "carry", "CARRY_BORROW_APR": "0.0",
                        "CARRY_EXECUTION_MODE": "overlay"})
    bot = _main.build_bot(config=cfg, store=_Store(), client=mock.Mock(),
                          risk_manager=mock.Mock(), engine=mock.Mock())
    assert bot.carry.require_capital_check is True
    assert bot.carry.leverage == pytest.approx(float(cfg.DEFAULT_LEVERAGE))
