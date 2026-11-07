"""Live authority comes from the venue registry, not only from flags: a sandbox or unknown venue never inherits it, and a live-armed process cannot run the net-benefit gate in observe-only mode."""
from __future__ import annotations

import dataclasses
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import config  # noqa: E402
import venues  # noqa: E402

LIVE = {"USE_TESTNET": "0", "PAPER_TRADING": "0", "LIVE_TRADING_ACK": "I_UNDERSTAND", "BYBIT_API_KEY": "k" * 12, "BYBIT_API_SECRET": "s" * 12}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith(("CARRY_", "BYBIT_", "LIVE_", "USE_TESTNET", "PAPER_")):
            monkeypatch.delenv(k, raising=False)


def test_the_registered_live_venue_with_all_four_parts_is_authorised_positive_control():
    ok, why = config.is_live_authorized({**LIVE, "BYBIT_VENUE": "mainnet"})
    assert ok is True, why


@pytest.mark.parametrize("venue", ["eu", "demo", "testnet", "bogus", "", "MAINNET.evil"])
def test_a_mapping_naming_any_other_venue_is_never_live(venue):
    ok, why = config.is_live_authorized({**LIVE, "BYBIT_VENUE": venue})
    assert ok is False and "venue" in why


def test_a_mapping_that_names_no_venue_keeps_the_four_part_rule_unchanged():
    assert config.is_live_authorized({**LIVE})[0] is True
    assert config.is_live_authorized({**LIVE, "LIVE_TRADING_ACK": ""})[0] is False


def test_a_config_replaced_onto_a_sandbox_venue_loses_live_authority():
    base = config.load({"BYBIT_VENUE": "testnet", "USE_TESTNET": "1"})
    forged = dataclasses.replace(base, USE_TESTNET=False, PAPER_TRADING=False, LIVE_TRADING_ACK="I_UNDERSTAND",
                                 BYBIT_API_KEY="k" * 12, BYBIT_API_SECRET="s" * 12, LIVE_AUTHORIZED=True, BYBIT_VENUE="demo")
    assert config.is_live_authorized(forged)[0] is False


def test_live_armed_configuration_refuses_observe_only_entry_gate():
    env = {**LIVE, "BYBIT_VENUE": "mainnet"}
    with pytest.raises(config.ConfigError, match="observe"):
        config.load({**env, "CARRY_ENTRY_BENEFIT_MODE": "observe"})
    config.load({**env, "CARRY_ENTRY_BENEFIT_MODE": "enforce"})                      # control: the same live env with enforce loads
    config.load({"CARRY_ENTRY_BENEFIT_MODE": "observe"})                              # sandbox may observe


def test_live_names_are_exactly_the_registry_live_set():
    assert set(venues.live_names()) == {"mainnet"}
