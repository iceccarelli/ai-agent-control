"""``is_live_authorized`` must not trust a stale stored flag on a ``Config``.

``Config`` is a frozen dataclass whose ``LIVE_AUTHORIZED`` is computed once in
``load()``. ``dataclasses.replace`` copies it unchanged, so an authorised config
re-pointed at paper/testnet, or with its acknowledgement or credentials removed,
still reported live as armed. The gate now re-derives the verdict from the
fields the four-part rule is made of and requires it to agree with the stored
flag. Offline; the env mapping is passed in, nothing is read from the process.
"""
from __future__ import annotations

import dataclasses
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config  # noqa: E402

LIVE_ENV = {"USE_TESTNET": "0", "PAPER_TRADING": "0",
            "LIVE_TRADING_ACK": "I_UNDERSTAND", "BYBIT_API_KEY": "k",
            "BYBIT_API_SECRET": "s", "BYBIT_VENUE": "mainnet"}


@pytest.fixture()
def armed():
    cfg = config.load(dict(LIVE_ENV))
    assert cfg.LIVE_AUTHORIZED is True
    return cfg


def test_a_freshly_loaded_authorised_config_is_still_authorised(armed):
    assert config.is_live_authorized(armed)[0] is True


@pytest.mark.parametrize("over", [
    {"PAPER_TRADING": True}, {"USE_TESTNET": True},
    {"LIVE_TRADING_ACK": ""}, {"LIVE_TRADING_ACK": "yes"},
    {"BYBIT_API_KEY": ""}, {"BYBIT_API_SECRET": ""}])
def test_replacing_a_gate_field_revokes_authorisation(armed, over):
    stale = dataclasses.replace(armed, **over)
    assert stale.LIVE_AUTHORIZED is True          # the stored flag is stale
    allowed, why = config.is_live_authorized(stale)
    assert allowed is False and why


def test_a_config_loaded_unarmed_cannot_be_armed_by_replace_of_its_fields():
    cfg = config.load({"USE_TESTNET": "1", "PAPER_TRADING": "1"})
    forged = dataclasses.replace(cfg, USE_TESTNET=False, PAPER_TRADING=False,
                                 LIVE_TRADING_ACK="I_UNDERSTAND",
                                 BYBIT_API_KEY="k", BYBIT_API_SECRET="s")
    assert config.is_live_authorized(forged)[0] is False   # stored flag False
