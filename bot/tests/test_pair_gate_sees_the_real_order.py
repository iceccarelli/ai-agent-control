"""The pair gate judges the order the engine is about to send, not the engine's own cap."""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
for p in (os.path.dirname(HERE), HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import carry_risk                                                    # noqa: E402
from carry_engine import CarryEngine, BookState                      # noqa: E402


class Snap:
    perp_mark = 100_000.0
    spot_mark = 100_000.0
    margin_multiple = None
    basis_bps = 0.0
    detail = {}

    def assert_fresh(self):
        pass


def test_the_gate_refuses_an_order_above_the_cap_and_passes_one_at_it():
    g = carry_risk.CarryRisk(max_notional_usd=100.0)
    snap = Snap()
    assert g.gate_open(notional_usd=100.0, snapshot=snap, has_open_pair=False).ok
    assert g.gate_open(notional_usd=100.0 * 0.001 / 0.001, snapshot=snap, has_open_pair=False).ok
    d = g.gate_open(notional_usd=100.01, snapshot=snap, has_open_pair=False)
    assert not d.ok and d.reason == "NOTIONAL_ABOVE_CAP"
    # float noise in qty*mark is not a breach
    assert g.gate_open(notional_usd=0.001 * 100_000.0 * (1 + 1e-12), snapshot=snap, has_open_pair=False).ok


def test_the_engine_hands_the_gate_the_real_order_notional():
    from test_carry_fail_closed import Venue, engine, _print_ms, MARK

    class Recorder(carry_risk.CarryRisk):
        seen = []

        def gate_open(self, **kw):
            Recorder.seen.append(kw["notional_usd"])
            return super().gate_open(**kw)

    v = Venue()
    e = engine(v)
    e.pair_risk = Recorder(max_notional_usd=100.0)
    d = e.on_candle(mark=MARK, funding_bps=3.0, spot=MARK, funding_print_ms=_print_ms())
    assert d.action == "opened"
    # sized at 100 USD of BTC, floored to the 1e-6 lot: the order, not the constant
    qty = next(iter(v.orders))  # side/product only; the size is what the gate saw
    assert len(Recorder.seen) == 1
    assert 99.0 < Recorder.seen[0] <= 100.0 * (1 + 1e-9)


def test_a_sizing_bug_that_overshoots_the_cap_is_now_caught_by_the_gate(monkeypatch):
    """Engine cap and gate cap AGREE (as in production: one constant). A sizing bug
    that builds an order twice the cap used to sail through, because the gate was
    shown the engine's own cap constant. It is shown the real order now."""
    import carry_engine
    from test_carry_fail_closed import Venue, engine, _print_ms, MARK
    real = carry_engine.snap_to_lot
    monkeypatch.setattr(carry_engine, "snap_to_lot", lambda qty, step: 2.0 * real(qty, step))
    v = Venue()
    e = engine(v)
    assert e.max_notional_usd == 100.0 and e.pair_risk.max_notional_usd == 100.0
    d = e.on_candle(mark=MARK, funding_bps=3.0, spot=MARK, funding_print_ms=_print_ms())
    assert d.reason == "NOTIONAL_ABOVE_CAP" and v.orders == []
    assert 199.0 < d.detail["requested"] <= 200.0 * (1 + 1e-9)
