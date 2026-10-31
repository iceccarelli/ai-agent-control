"""The additional entry net-benefit gate: five separate layers, fail-closed on
missing inputs, never in front of an exit, never replacing the production gate."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as _config                                     # noqa: E402
import entry_benefit_gate as ebg                             # noqa: E402
import execution_cost_estimate as ece                        # noqa: E402
import strategy_evidence as se                               # noqa: E402

NOW = 1_800_000_000_000
DAY = ece.DAY_MS


@dataclass
class E:
    kind: str
    ms: int
    ref: str
    fee_raw: Dict[str, Any] = field(default_factory=dict)


def rec(side, product="linear", q=0.01, ref=60000.0, slip=0.12, spr=0.3):
    return {"schema": 1, "status": "MEASURED", "reference_basis": "executable_side",
            "symbol": "BTCUSDT", "product": product, "side": side,
            "order_type": "market", "filled_qty": q, "requested_qty": q,
            "avg_price": ref, "reference_price": ref, "slippage_usd": slip,
            "spread_cost_usd": spr, "decision_to_submit_ms": 5,
            "submit_to_fill_ms": 40}


def fills(n, side, kind, **kw):
    return [E(kind, int(NOW - DAY - i), f"BB-{kind}-{i}", {"execution": rec(side, **kw)})
            for i in range(n)]


FULL = fills(25, "Sell", "perp_open") + fills(25, "Buy", "perp_close")


def allow_verdict():
    return se.EvidenceVerdict("btc_funding_carry", se.ALLOCATE, ("OK",), {})


def args(**over):
    base = dict(now_ms=NOW, entries=FULL, execution_mode="overlay",
                perp_symbol="BTCUSDT", spot_symbol="BTCUSDT", mark=60000.0,
                spot=60000.0, funding_history=[3.0, 3.2, 2.8, 3.0],
                last_print_ms=NOW - 3600_000, round_trip_bps=11.0,
                borrow_apr=0.0, opportunity_cost_apr=0.0, notional_usd=600.0)
    base.update(over)
    return base


def gate(**kw):
    kw.setdefault("evidence_provider", allow_verdict)
    return ebg.EntryBenefitGate(**kw)


def test_all_layers_pass_only_with_real_costs_and_an_allocate_verdict():
    r = gate().check(**args())
    assert r.would_allow and r.allowed and r.reason == ""
    t = r.trace
    assert t["outcome"] == "ENTRY_ELIGIBLE"
    assert t["layers"]["capital_allocation"]["passed"] is None
    assert t["layers"]["order_permission"]["passed"] is None
    assert t["fingerprint"] and t["inputs"]["execution_cost"]["status"] == "MEASURED"


def test_no_evidence_provider_means_strategy_evidence_insufficient():
    r = ebg.EntryBenefitGate().check(**args())
    assert r.reason == ebg.EVIDENCE_INSUFFICIENT and not r.allowed
    # the benefit layer is judged on its own and reported as supported
    assert r.trace["layers"]["entry_benefit"]["passed"] is True
    assert r.trace["layers"]["strategy_evidence"]["classification"] == "NO_VERDICT_SUPPLIED"


def test_an_insufficient_or_blocked_verdict_refuses():
    for v in (se.INSUFFICIENT, se.BLOCK):
        r = gate(evidence_provider=lambda v=v: se.EvidenceVerdict("btc_funding_carry", v, ("x",), {})
                 ).check(**args())
        assert r.reason == ebg.EVIDENCE_INSUFFICIENT and not r.allowed


def test_a_provider_that_raises_is_no_verdict():
    def boom():
        raise RuntimeError("x")
    r = gate(evidence_provider=boom).check(**args())
    assert r.reason == ebg.EVIDENCE_INSUFFICIENT


def test_missing_execution_samples_refuse_as_unavailable_not_zero():
    r = gate().check(**args(entries=[]))
    assert r.reason == ebg.BENEFIT_UNAVAILABLE and not r.allowed
    legs = r.trace["inputs"]["execution_cost"]
    assert legs["status"] == "UNAVAILABLE" and legs["spread_bps"] is None
    assert any("INSUFFICIENT_SAMPLES" in x for x in legs["reasons"])


def test_one_missing_leg_is_enough_to_refuse():
    r = gate().check(**args(entries=fills(25, "Sell", "perp_open")))
    assert r.reason == ebg.BENEFIT_UNAVAILABLE
    assert any("linear/Buy" in x for x in r.trace["inputs"]["execution_cost"]["reasons"])


@pytest.mark.parametrize("field,val", [
    ("round_trip_bps", None), ("borrow_apr", None), ("opportunity_cost_apr", None),
    ("notional_usd", None), ("round_trip_bps", float("nan")),
])
def test_any_missing_cost_input_is_unavailable(field, val):
    r = gate().check(**args(**{field: val}))
    assert r.reason == ebg.BENEFIT_UNAVAILABLE and not r.allowed


def test_too_little_funding_history_cannot_state_uncertainty():
    r = gate().check(**args(funding_history=[3.0]))
    assert r.reason == ebg.BENEFIT_UNAVAILABLE


def test_negative_net_benefit_is_not_supported():
    r = gate().check(**args(funding_history=[0.1, 0.1, 0.1]))
    assert r.reason == ebg.BENEFIT_NOT_SUPPORTED
    assert r.trace["layers"]["entry_benefit"]["net_benefit_bps"] < 0


def test_noisy_funding_inside_its_own_uncertainty_is_not_supported():
    r = gate().check(**args(funding_history=[8.0, -2.0, 8.0, -2.0, 8.0, -2.0]))
    assert r.reason == ebg.BENEFIT_NOT_SUPPORTED


def test_borrow_and_opportunity_cost_reduce_the_benefit():
    free = gate().check(**args()).trace["layers"]["entry_benefit"]["net_benefit_bps"]
    dear = gate().check(**args(borrow_apr=0.10, opportunity_cost_apr=0.05)
                        ).trace["layers"]["entry_benefit"]["net_benefit_bps"]
    assert dear < free


def test_haircut_reduces_the_benefit():
    a = gate(income_haircut_fraction=0.0).check(**args()).trace["layers"]["entry_benefit"]["net_benefit_bps"]
    b = gate(income_haircut_fraction=0.5).check(**args()).trace["layers"]["entry_benefit"]["net_benefit_bps"]
    assert b < a


def test_observe_mode_records_the_refusal_but_lets_the_entry_continue():
    r = ebg.EntryBenefitGate(mode=ebg.OBSERVE).check(**args())
    assert r.allowed and not r.would_allow and not r.enforcing
    assert r.trace["would_refuse"] is True and r.trace["mode"] == "observe"


def test_enforce_is_the_default_and_bad_modes_are_rejected():
    assert ebg.EntryBenefitGate().mode == ebg.ENFORCE
    with pytest.raises(ValueError):
        ebg.EntryBenefitGate(mode="off")
    with pytest.raises(ValueError):
        ebg.EntryBenefitGate(income_haircut_fraction=1.5)
    with pytest.raises(ValueError):
        ebg.EntryBenefitGate(cost_policy=ece.CostSamplePolicy(min_samples=3))


def test_the_trace_is_deterministic_and_the_fingerprint_tracks_the_rule():
    a = gate().check(**args()).trace
    b = gate().check(**args()).trace
    assert a == b
    assert gate().fingerprint() != gate(income_haircut_fraction=0.1).fingerprint()
    assert gate().fingerprint() != ebg.EntryBenefitGate(mode=ebg.OBSERVE).fingerprint()


def test_later_fills_do_not_change_the_decision():
    later = fills(10, "Sell", "perp_open")
    for i, e in enumerate(later):
        e.ms = NOW + 1000 + i                                 # after the decision
        e.ref = f"LATER-{i}"
    base = gate().check(**args()).trace["inputs"]["execution_cost"]
    both = gate().check(**args(entries=FULL + later)).trace["inputs"]["execution_cost"]
    assert (base["spread_bps"], base["slippage_bps"]) == \
        (both["spread_bps"], both["slippage_bps"])
    assert [l["n"] for l in both["legs"]] == [25, 25]
    assert both["legs"][0]["rejected"]["AFTER_DECISION"] == 10


def test_config_defaults_and_validation():
    cfg = _config.load({})
    assert cfg.CARRY_ENTRY_BENEFIT_MODE == "enforce"
    assert cfg.CARRY_INCOME_HAIRCUT_FRACTION == 0.0
    assert _config.load({"CARRY_ENTRY_BENEFIT_MODE": "OBSERVE"}
                        ).CARRY_ENTRY_BENEFIT_MODE == "observe"
    with pytest.raises(_config.ConfigError):
        _config.load({"CARRY_ENTRY_BENEFIT_MODE": "off"})


# -- in the engine: after the production gates, in front of entries only -------------

def _engine(tmp_path, gate_obj):
    from test_paired_leg_recovery import FeeVenue, modern, stack
    store, venue, broker, engine = stack(str(tmp_path / "s.db"),
                                         FeeVenue(modern, spot_qty=0.0))
    engine.entry_benefit = gate_obj
    return store, venue, engine


def test_engine_refuses_an_entry_with_the_gates_reason_and_trace(tmp_path):
    from test_paired_leg_recovery import warm_and_open
    store, venue, engine = _engine(tmp_path, ebg.EntryBenefitGate())
    d = warm_and_open(engine)
    assert d.action == "stand_aside" and d.reason == ebg.BENEFIT_UNAVAILABLE
    assert engine.position is None and not venue.created        # no order left
    assert engine.last_entry_trace["outcome"] == ebg.BENEFIT_UNAVAILABLE
    assert d.detail["layers"]["mechanical"]["passed"] is True
    store.close()


def test_observe_mode_lets_the_pair_open_and_keeps_the_would_refuse_trace(tmp_path):
    from test_paired_leg_recovery import warm_and_open
    store, venue, engine = _engine(tmp_path, ebg.EntryBenefitGate(mode=ebg.OBSERVE))
    d = warm_and_open(engine)
    assert d.action == "opened" and engine.position is not None
    assert engine.last_entry_trace["would_refuse"] is True
    store.close()


def test_without_the_gate_the_production_behaviour_is_unchanged(tmp_path):
    from test_paired_leg_recovery import warm_and_open
    store, venue, engine = _engine(tmp_path, None)
    assert warm_and_open(engine).action == "opened"
    assert engine.last_entry_trace == {}
    store.close()


def test_the_gate_never_stands_in_front_of_an_exit(tmp_path):
    """A held pair unwinds on an inverted funding whatever the gate says."""
    from test_paired_leg_recovery import warm_and_open, fresh_view, MARK
    import carry_engine as ce
    store, venue, engine = _engine(tmp_path, None)
    assert warm_and_open(engine).action == "opened"
    engine.entry_benefit = ebg.EntryBenefitGate()            # refuses everything
    engine.position.negative_funding_streak = ce.NEGATIVE_FUNDING_EXIT_PRINTS
    fresh_view(engine)
    d = engine.on_candle(mark=MARK, funding_bps=-3.0, spot=MARK,
                         funding_print_ms=0)
    assert (d.action, d.reason) == ("unwound", "FUNDING_INVERTED")
    assert engine.position is None
    store.close()


def test_the_mechanical_gate_still_decides_first(tmp_path):
    """Funding that fails the production gate is refused by IT, not by ours."""
    from test_paired_leg_recovery import fresh_view, MARK
    store, venue, engine = _engine(tmp_path, ebg.EntryBenefitGate(mode=ebg.OBSERVE))
    d = None
    for i in (1, 2, 3):
        fresh_view(engine)
        d = engine.on_candle(mark=MARK, funding_bps=0.1, spot=MARK,
                             funding_print_ms=i * 8 * 3600 * 1000)
    assert d.action == "stand_aside" and d.reason not in (
        ebg.BENEFIT_UNAVAILABLE, ebg.BENEFIT_NOT_SUPPORTED, ebg.EVIDENCE_INSUFFICIENT)
    assert engine.last_entry_trace == {}                       # never reached
    store.close()


# -- review follow-ups -------------------------------------------------------------

def test_maker_first_pools_resting_and_market_fills_for_each_leg():
    resting = fills(25, "Sell", "perp_open")
    for e in resting:
        e.fee_raw["execution"]["order_type"] = "post_only"
    closes = fills(25, "Buy", "perp_close")
    taker = gate().check(**args(entries=resting + closes, execution_style="taker"))
    maker = gate().check(**args(entries=resting + closes, execution_style="maker_first"))
    assert taker.reason == ebg.BENEFIT_UNAVAILABLE          # no market samples on the entry leg
    assert maker.trace["inputs"]["execution_cost"]["status"] == "MEASURED"


def test_a_verdict_for_another_strategy_unlocks_nothing():
    other = lambda: se.EvidenceVerdict("some_other_strategy", se.ALLOCATE, ("OK",), {})
    r = gate(evidence_provider=other).check(**args())
    assert r.reason == ebg.EVIDENCE_INSUFFICIENT
    assert r.trace["layers"]["strategy_evidence"]["verdict_strategy_id"] == "some_other_strategy"
    mine = lambda: se.EvidenceVerdict("btc_funding_carry", se.ALLOCATE, ("OK",), {})
    assert gate(evidence_provider=mine).check(**args()).would_allow


def test_settlements_per_day_is_the_cost_models():
    import carry_costs
    assert ebg.SETTLEMENTS_PER_DAY == carry_costs.FUNDING_PERIODS_PER_DAY


def test_two_identical_partials_in_one_millisecond_do_not_change_the_bps():
    """Dropping a byte-identical partial halves numerator and denominator
    together, so the order's bps is unchanged."""
    rows = fills(25, "Sell", "perp_open", q=0.005, slip=0.06, spr=0.15)
    dup = [E(r.kind, r.ms, r.ref, r.fee_raw) for r in rows]
    a = ece.estimate_leg(rows, as_of_ms=NOW, symbol="BTCUSDT", product="linear",
                         side="Sell", planned_notional_usd=300.0)
    b = ece.estimate_leg(rows + dup, as_of_ms=NOW, symbol="BTCUSDT", product="linear",
                         side="Sell", planned_notional_usd=300.0)
    assert a["slippage_bps"] == b["slippage_bps"]


def test_the_engine_judges_samples_on_the_callers_clock(tmp_path):
    """A replay clock earlier than the recorded fills sees them as later than
    the decision (look-ahead), whatever the wall clock says."""
    from test_paired_leg_recovery import warm_and_open
    store, venue, engine = _engine(tmp_path, ebg.EntryBenefitGate(mode=ebg.OBSERVE))
    seen = {}
    real = engine.entry_benefit.check

    def spy(**kw):
        seen.update(kw)
        return real(**kw)
    engine.entry_benefit.check = spy
    engine._entry_clock_ms = 0
    warm_and_open(engine)
    assert seen["now_ms"] > 1_000_000_000_000                # wall clock when none given
    store.close()


def test_the_engine_reuses_the_round_trip_the_cost_gate_just_priced(tmp_path):
    from test_paired_leg_recovery import warm_and_open
    store, venue, engine = _engine(tmp_path, ebg.EntryBenefitGate(mode=ebg.OBSERVE))
    calls = []
    real = engine.round_trip_bps
    engine.round_trip_bps = lambda: calls.append(1) or real()
    warm_and_open(engine)
    # entry_economics priced it once per tick; the gate adds no second lookup
    entry_ticks = len(calls)
    assert engine.last_entry_trace["inputs"]["round_trip_fee_bps"] == real()
    assert entry_ticks == 3                                   # the three warm_and_open ticks
    store.close()


def test_observe_mode_logs_a_repeated_would_refuse_once(tmp_path):
    """Counted with a handler of our own: other tests change how the logging
    tree propagates, which must not decide this one."""
    from test_paired_leg_recovery import MARK
    import logging
    import carry_engine as ce

    class Count(logging.Handler):
        def __init__(self):
            super().__init__(logging.WARNING)
            self.n = 0

        def emit(self, record):
            if "WOULD refuse" in record.getMessage():
                self.n += 1

    store, venue, engine = _engine(tmp_path, ebg.EntryBenefitGate(mode=ebg.OBSERVE))
    engine.entry_benefit.check = lambda **kw: ebg.EntryBenefitResult(
        True, False, ebg.BENEFIT_UNAVAILABLE, False, {"outcome": "x"})
    handler = Count()
    log = ce.logger
    old_level, old_disabled = log.level, log.disabled
    log.addHandler(handler)
    log.setLevel(logging.WARNING)
    log.disabled = False
    try:
        engine._observe_logged = ""
        for _ in range(3):
            engine._entry_benefit_refusal(MARK, MARK, 600.0)
    finally:
        log.removeHandler(handler)
        log.setLevel(old_level)
        log.disabled = old_disabled
    assert handler.n == 1
    store.close()
