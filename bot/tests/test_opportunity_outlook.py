"""The book states what it is waiting for, from the same gate it enters on.
The outlook is informational: mechanical eligibility only, never a permission."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import carry_costs as cc                                     # noqa: E402
import carry_engine as ce                                    # noqa: E402
from test_paired_leg_recovery import FeeVenue, MARK, modern, stack, warm_and_open  # noqa: E402

KW = dict(perp=60000.0, spot=60000.0, borrow_apr=0.05, round_trip_bps=30.0)
NOW = 1_800_000_000_000
H8 = 8 * 3600 * 1000


def passes(f, **kw):
    return bool(cc.entry_gate(funding_prints_bps=[f] * 3, **{**KW, **kw}))


@pytest.fixture()
def eng(tmp_path):
    store, venue, broker, engine = stack(str(tmp_path / "s.db"),
                                         FeeVenue(modern, spot_qty=0.0))
    yield engine
    store.close()


def seed(engine, rates, last_ms=NOW - 3600_000, round_trip=None):
    engine._funding_history = list(rates)
    engine._last_print_ms = last_ms
    # what the production entry path would have priced and remembered
    engine._last_round_trip = (engine.round_trip_bps() if round_trip is None
                               else round_trip, NOW - 1000)


# -- the threshold is the production gate's edge ----------------------------------

def test_breakeven_is_the_edge_of_the_production_gate():
    need = cc.breakeven_funding_bps(**KW)
    assert need is not None and need > 0
    assert passes(need + 1e-6) and not passes(need - 1e-3)


def test_boundaries_invalid_inputs_and_ceiling():
    # a zero round trip is an input the gate cannot price: no solution, and the
    # gate itself says why
    zero = {**KW, "borrow_apr": 0.0, "round_trip_bps": 0.0}
    assert cc.breakeven_funding_bps(**zero) is None
    assert cc.entry_gate(funding_prints_bps=[5.0] * 3, **zero).reason == \
        "COST_INPUT_INVALID"
    # a ceiling below the need is "no solution", not the ceiling
    need = cc.breakeven_funding_bps(**KW)
    assert cc.breakeven_funding_bps(**KW, ceiling_bps=need / 2.0) is None
    # exactly at the ceiling that equals the need (within bisection width)
    assert cc.breakeven_funding_bps(**KW, ceiling_bps=need + 1e-6) is not None


def test_higher_costs_need_more_funding():
    base = cc.breakeven_funding_bps(**KW)
    assert cc.breakeven_funding_bps(**{**KW, "round_trip_bps": 60.0}) > base
    assert cc.breakeven_funding_bps(**{**KW, "borrow_apr": 0.15}) > base
    assert cc.breakeven_funding_bps(**{**KW, "opportunity_cost_apr": 0.05}) > base


def test_no_funding_clears_an_absurd_cost():
    assert cc.breakeven_funding_bps(**{**KW, "round_trip_bps": 5000.0}) is None


# -- status ------------------------------------------------------------------------

def test_waits_for_funding_below_the_need(eng):
    seed(eng, [0.1, 0.1, 0.1])
    o = eng.opportunity_outlook(mark=MARK, spot=MARK, now_ms=NOW)
    assert o["status"] == "WAITING_FOR_FUNDING"
    assert o["gap_bps_per_print"] > 0 and not o["holding"]
    assert o["mechanical_entry_eligible"] is False


def test_conditions_met_is_mechanical_only(eng):
    seed(eng, [3.0, 3.0, 3.0])
    o = eng.opportunity_outlook(mark=MARK, spot=MARK, now_ms=NOW)
    assert o["status"] == "ENTRY_CONDITIONS_MET"
    assert o["mechanical_entry_eligible"] is True
    ne = o["not_evaluated"]
    assert {"strategy_evidence", "capital_allocation", "order_permission"} <= set(ne)
    assert "not a trade signal" in ne["note"]


def test_status_agrees_with_the_real_gate_on_the_real_history(eng):
    for hist in ([0.1] * 3, [1.0] * 3, [3.0] * 3, [3.0, 0.0, 3.0, 0.0]):
        seed(eng, hist)
        o = eng.opportunity_outlook(mark=MARK, spot=MARK, now_ms=NOW)
        real = cc.entry_gate(
            funding_prints_bps=hist, perp=MARK, spot=MARK,
            borrow_apr=eng.borrow_apr, round_trip_bps=eng._last_round_trip[0],
            opportunity_cost_apr=eng.opportunity_cost_apr)
        assert o["mechanical_entry_eligible"] == bool(real), (hist, o)
        assert o["entry_gate_reason"] == real.reason


def test_missing_history_waits_and_says_how_many(eng):
    seed(eng, [3.0, 3.0])
    o = eng.opportunity_outlook(mark=MARK, spot=MARK, now_ms=NOW)
    assert o["status"] == "WAITING_FOR_FUNDING_HISTORY"
    assert o["funding_evidence"]["sample_count"] == 2
    assert o["smoothed_funding_bps"] is None


def test_stale_funding_waits_instead_of_trusting_old_prints(eng):
    seed(eng, [3.0, 3.0, 3.0], last_ms=NOW - 2 * H8)
    o = eng.opportunity_outlook(mark=MARK, spot=MARK, now_ms=NOW)
    assert o["status"] == "WAITING_FOR_FUNDING_HISTORY"
    assert "STALE" in o["why"] and o["mechanical_entry_eligible"] is False


def test_unknown_print_age_is_not_fresh(eng):
    seed(eng, [3.0, 3.0, 3.0], last_ms=None)
    o = eng.opportunity_outlook(mark=MARK, spot=MARK, now_ms=NOW)
    assert o["status"] == "WAITING_FOR_FUNDING_HISTORY"
    assert o["funding_evidence"]["age_s"] is None


def test_impossible_costs_report_no_solution(eng):
    seed(eng, [3.0, 3.0, 3.0], round_trip=5000.0)
    o = eng.opportunity_outlook(mark=MARK, spot=MARK, now_ms=NOW)
    assert o["status"] == "NO_FUNDING_LEVEL_CLEARS_THE_COSTS"
    assert o["funding_needed_bps_per_print"] is None


def test_costs_not_yet_priced_are_unknown_not_zero_and_the_venue_is_not_asked(eng):
    class Spy:
        def __getattr__(self, name):
            raise AssertionError(f"outlook touched the venue: {name}")
    real = eng.broker
    seed(eng, [3.0, 3.0, 3.0])
    eng._last_round_trip = None
    eng.broker = Spy()
    try:
        o = eng.opportunity_outlook(mark=MARK, spot=MARK, now_ms=NOW)
    finally:
        eng.broker = real
    assert o["status"] == "UNKNOWN" and "NOT_YET_READ" in o["why"]
    assert o["mechanical_entry_eligible"] is False


def test_missing_spot_is_unavailable(eng):
    seed(eng, [3.0, 3.0, 3.0])
    o = eng.opportunity_outlook(mark=MARK, spot=None, now_ms=NOW)
    assert o["why"] == "SPOT_OR_MARK_UNAVAILABLE" and o["status"] == "UNKNOWN"


def test_units_are_stated_and_funding_is_not_annualised(eng):
    seed(eng, [3.0, 3.0, 3.0])
    o = eng.opportunity_outlook(mark=MARK, spot=MARK, now_ms=NOW)
    assert "per 8h settlement" in o["units"]["funding"]
    assert abs(o["smoothed_funding_bps"] - 3.0) < 1e-9      # bps/print, as fed


def test_outlook_is_deterministic(eng):
    seed(eng, [1.0, 2.0, 3.0])
    a = eng.opportunity_outlook(mark=MARK, spot=MARK, now_ms=NOW)
    b = eng.opportunity_outlook(mark=MARK, spot=MARK, now_ms=NOW)
    assert a == b


# -- holding and exit rules --------------------------------------------------------

def test_exit_rules_match_the_engines_constants(eng):
    o = eng.opportunity_outlook(mark=MARK, spot=MARK, now_ms=NOW)
    rules = {r["id"]: r for r in o["exit"]["rules"]}
    assert rules["FUNDING_INVERTED"]["threshold"] == ce.NEGATIVE_FUNDING_EXIT_PRINTS
    assert rules["MARGIN_HEADROOM"]["threshold"] == eng.min_margin_multiple
    assert rules["LIQUIDATION_DISTANCE"]["threshold"] == ce.MIN_LIQUIDATION_DISTANCE_PCT
    assert rules["DELTA_DRIFT"]["threshold"] == eng.delta_band
    # a delta breach rebalances; it is not an exit
    assert "not an exit" in rules["DELTA_DRIFT"]["action"]
    assert o["exit"]["active_exit_triggers"] == []            # nothing held


def test_holding_reports_active_triggers_from_state(eng):
    warm_and_open(eng)
    eng.position.negative_funding_streak = ce.NEGATIVE_FUNDING_EXIT_PRINTS
    o = eng.opportunity_outlook(mark=MARK, spot=MARK, now_ms=NOW)
    assert o["status"] == "HOLDING"
    assert "FUNDING_INVERTED" in o["exit"]["active_exit_triggers"]
    eng.position.negative_funding_streak = 0
    o = eng.opportunity_outlook(mark=MARK, spot=MARK, now_ms=NOW)
    assert "FUNDING_INVERTED" not in o["exit"]["active_exit_triggers"]


def test_the_outlook_changes_no_state_and_sends_nothing(eng):
    seed(eng, [3.0, 3.0, 3.0])
    before = (list(eng._funding_history), eng.position, eng.state)
    eng.opportunity_outlook(mark=MARK, spot=MARK, now_ms=NOW)
    assert (list(eng._funding_history), eng.position, eng.state) == before


def test_a_costly_basis_raises_the_funding_needed(eng):
    """The need is computed at the CURRENT basis, so a rich or discounted
    basis shows up as more funding required, with the gate's own reason."""
    seed(eng, [3.0, 3.0, 3.0])
    flat = eng.opportunity_outlook(mark=60000.0, spot=60000.0, now_ms=NOW)
    wide = eng.opportunity_outlook(mark=60000.0, spot=57000.0, now_ms=NOW)
    assert wide["funding_needed_bps_per_print"] > flat["funding_needed_bps_per_print"]
    assert wide["status"] == "WAITING_FOR_FUNDING"
    assert wide["entry_gate_reason"] == "ENTRY_BASIS_EXCEEDS_CARRY_BUDGET"
    assert wide["mechanical_entry_eligible"] is False


def test_an_unpriceable_cost_input_is_a_refusal_not_no_solution(eng):
    seed(eng, [3.0, 3.0, 3.0], round_trip=0.0)
    o = eng.opportunity_outlook(mark=MARK, spot=MARK, now_ms=NOW)
    assert o["status"] == "ENTRY_GATE_REFUSED"
    assert o["why"] == "COST_INPUT_INVALID"


def test_the_production_entry_path_remembers_the_price_it_used(eng):
    eng._funding_history = [3.0, 3.0, 3.0]
    assert eng._last_round_trip is None
    eng.entry_economics(mark=MARK, spot=MARK, funding_bps=3.0)
    assert eng._last_round_trip is not None
    assert eng._last_round_trip[0] == eng.round_trip_bps()


def test_carry_status_carries_the_outlook_and_keeps_its_existing_fields(eng, tmp_path):
    import main as _main
    from test_paired_leg_recovery import _bot_on
    from persistence import StateStore
    store = StateStore(str(tmp_path / "st.db"))
    bot = _bot_on(store, eng)
    bot.cfg = type("C", (), {"USE_TESTNET": True, "PAPER_TRADING": True})()
    bot._last_carry_decision = {}
    bot._journal_unsaved = False
    seed(eng, [3.0, 3.0, 3.0])
    status = bot.carry_status()
    assert "market_view" in status["outlook"]
    for key in ("attached", "environment", "book_state", "last_decision",
                "protective_stop", "journal_unsaved"):
        assert key in status, key
    assert status["outlook"]["status"] in {
        "UNKNOWN", "WAITING_FOR_FUNDING_HISTORY", "WAITING_FOR_FUNDING",
        "ENTRY_CONDITIONS_MET", "ENTRY_GATE_REFUSED", "HOLDING",
        "NO_FUNDING_LEVEL_CLEARS_THE_COSTS"}
    store.close()


def test_an_old_round_trip_price_is_not_used(eng):
    seed(eng, [3.0, 3.0, 3.0])
    eng._last_round_trip = (30.0, NOW - eng.OUTLOOK_COST_MAX_AGE_MS - 1)
    o = eng.opportunity_outlook(mark=MARK, spot=MARK, now_ms=NOW)
    assert "STALE" in o["why"] and o["mechanical_entry_eligible"] is False
    eng._last_round_trip = (30.0, NOW - 1000)
    assert eng.opportunity_outlook(mark=MARK, spot=MARK,
                                   now_ms=NOW)["mechanical_entry_eligible"] is True
