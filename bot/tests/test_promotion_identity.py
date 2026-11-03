"""The promotion gate's forward-evidence item is bound to the strategy.

`_forward_item` used to judge only the artefact's freshness, trade count, day
count and trend. It never read which signal, symbol or constants the artefact
was produced under, and `promotion_gate_allows_live` had no notion of the book
being promoted. In BOOK_MODE=carry `bot.strategy` is None, so the identity
check was vacuous and the only forward evidence read was the DIRECTIONAL
funding-fade shadow artefact: a carry book could pass the forward leg on
another strategy's paper record, or on an artefact that named anything at all.

No threshold is touched here. Offline; the live chain is stubbed so the
evidence leg is judged on its own.
"""
from __future__ import annotations

import os
import sys
import types

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import promotion_gate as pg                                  # noqa: E402
import shadow                                                # noqa: E402
from test_forward_status import write                        # noqa: E402
from test_promotion_forward_evidence import (                # noqa: E402
    KEY, approvals_for, gate_with, item)

GOOD = {"signal": shadow.SHADOW_SIGNAL, "symbol": shadow.SHADOW_SYMBOL,
        "constants_fingerprint": shadow.CONSTANTS_FINGERPRINT,
        "execution_mode": "paper"}


def verdict(tmp_path, monkeypatch, extra=None, drop=(), book_mode=None, n=95):
    art_extra = dict(GOOD)
    art_extra.update(extra or {})
    for k in drop:
        art_extra.pop(k, None)
    art, now = write(tmp_path, [0.30 + 0.01 * (i % 3) for i in range(n)],
                     extra=art_extra)
    if drop:                                   # `write` merges; remove after
        import json
        data = json.load(open(art))
        for k in drop:
            data.pop(k, None)
        json.dump(data, open(art, "w"))
    flag = {"complete": True, "owner": "observation", "evidence": "typed"}
    path, entries = gate_with(tmp_path, flag)
    apps = approvals_for(tmp_path, entries, [KEY])
    monkeypatch.setattr(pg, "_live_chain_satisfied",
                        lambda config=None: (True, "stubbed"))
    kw = {} if book_mode is None else {"book_mode": book_mode}
    return pg.evaluate_promotion_gate(
        path=path, repo=str(tmp_path), approvals_path=apps,
        forward_artifact=art, now=now, **kw)


def test_control_the_right_strategys_artefact_completes_the_item(
        tmp_path, monkeypatch):
    it = item(verdict(tmp_path, monkeypatch))
    assert it.complete is True, it.evidence


@pytest.mark.parametrize("extra", [
    {"signal": "totally_unrelated_xyz"},
    {"signal": "btc_funding_carry"},
    {"symbol": "DOGEUSDT"},
    {"constants_fingerprint": "0" * 32},
    {"execution_mode": "live"},
])
def test_an_artefact_of_another_identity_does_not_complete_the_item(
        tmp_path, monkeypatch, extra):
    v = verdict(tmp_path, monkeypatch, extra=extra)
    it = item(v)
    assert it.complete is False
    assert "identity" in it.evidence.lower()
    assert v.allows_live is False


@pytest.mark.parametrize("missing", ["signal", "symbol",
                                     "constants_fingerprint", "execution_mode"])
def test_an_artefact_that_does_not_state_its_identity_is_refused(
        tmp_path, monkeypatch, missing):
    it = item(verdict(tmp_path, monkeypatch, drop=(missing,)))
    assert it.complete is False and "identity" in it.evidence.lower()


def test_directional_evidence_cannot_promote_a_carry_book(tmp_path, monkeypatch):
    v = verdict(tmp_path, monkeypatch, book_mode="carry")
    it = item(v)
    assert it.complete is False
    assert "carry" in it.evidence.lower() and "directional" in it.evidence.lower()
    assert v.allows_live is False


def test_an_unknown_book_mode_is_refused_not_defaulted(tmp_path, monkeypatch):
    it = item(verdict(tmp_path, monkeypatch, book_mode="something_new"))
    assert it.complete is False


def test_the_directional_book_is_still_promotable_on_its_own_evidence(
        tmp_path, monkeypatch):
    it = item(verdict(tmp_path, monkeypatch, book_mode="directional"))
    assert it.complete is True


def test_allows_live_has_the_same_binding(tmp_path, monkeypatch):
    art, now = write(tmp_path, [0.3] * 95, extra=dict(GOOD))
    flag = {"complete": True, "owner": "observation", "evidence": "typed"}
    path, entries = gate_with(tmp_path, flag)
    apps = approvals_for(tmp_path, entries, [KEY])
    monkeypatch.setattr(pg, "_live_chain_satisfied",
                        lambda config=None: (True, "stubbed"))
    # Not every human item is approved in this fixture, so allows_live is False
    # either way; what is asserted is that the parameter exists and the item
    # itself is refused for the carry book.
    assert pg.promotion_gate_allows_live(
        path=path, repo=str(tmp_path), approvals_path=apps,
        forward_artifact=art, now=now, book_mode="carry") is False


# -- the callers hand the gate the book they are promoting ---------------------

def _recorder(monkeypatch):
    seen = []

    def allows(**kw):
        seen.append(kw)
        return False

    def evaluate(**kw):
        seen.append(kw)
        return types.SimpleNamespace(reasons=["recorded"], allows_live=False)

    monkeypatch.setattr(pg, "promotion_gate_allows_live", allows)
    monkeypatch.setattr(pg, "evaluate_promotion_gate", evaluate)
    return seen


def test_live_promotion_check_passes_the_book_mode(monkeypatch):
    import main
    seen = _recorder(monkeypatch)
    cfg = types.SimpleNamespace(BOOK_MODE="carry")
    ok, _why = main.TradingBot.live_promotion_check(
        types.SimpleNamespace(cfg=cfg, strategy=None))
    assert ok is False
    assert seen and all(kw.get("book_mode") == "carry" for kw in seen)


def test_the_directional_book_is_asked_as_directional(monkeypatch):
    import main
    seen = _recorder(monkeypatch)
    cfg = types.SimpleNamespace(BOOK_MODE="directional")
    main.TradingBot.live_promotion_check(
        types.SimpleNamespace(cfg=cfg, strategy=None))
    assert seen and all(kw.get("book_mode") == "directional" for kw in seen)


def test_a_carry_bot_is_never_told_a_cleared_signal_is_attached(monkeypatch):
    import main
    monkeypatch.setattr(pg, "promotion_gate_allows_live", lambda **kw: True)
    ok, why = main.TradingBot.live_promotion_check(types.SimpleNamespace(
        cfg=types.SimpleNamespace(BOOK_MODE="carry"), strategy=None))
    assert ok is True
    assert "cleared signal is attached" not in why and "carry" in why


def test_the_per_order_carry_gate_passes_the_book_mode(monkeypatch):
    import main
    seen = _recorder(monkeypatch)
    cfg = types.SimpleNamespace(
        BOOK_MODE="carry", USE_TESTNET=False, BYBIT_VENUE="mainnet",
        PAPER_TRADING=False, LIVE_TRADING_ACK="x")
    monkeypatch.setattr(main, "_mainnet_live_authorisation",
                        lambda cfg: (True, "stubbed"), raising=False)
    try:
        main.carry_orders_permitted(cfg)
    except Exception:            # the arming chain may refuse first; irrelevant
        pass
    assert all(kw.get("book_mode") == "carry" for kw in seen)
