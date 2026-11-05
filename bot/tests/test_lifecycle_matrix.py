"""The order-lifecycle scenarios an adversarial reviewer must be able to point at, and the
independence of the money path from the analytics path.

Two guards, both structural:

1. MATRIX: every required lifecycle scenario maps to named tests that must keep existing. The
   tests themselves are run by the ordinary suite; this file fails if one is renamed or deleted,
   which would silently reopen the scenario.
2. INDEPENDENCE: the modules that decide, send, close, recover and book orders import none of the
   recording, evidence-statistics, economics-model or opportunity modules. An emergency close
   cannot depend on a recorder that has stopped.
"""
import ast
import os

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)

MATRIX = {
    "timeout after venue acceptance / lost acknowledgement": [
        ("test_close_execution_faults.py", "test_a_lost_acknowledgement_is_found_by_lookup_and_nothing_is_sent_twice"),
        ("test_open_crash_real_stack.py", "test_a_lost_acknowledgement_is_read_back_and_booked_once_without_a_second_order")],
    "duplicate submission / reused id": [
        ("test_bybit_connection.py", "test_exchange_rejects_a_duplicate_link_id"),
        ("test_open_crash_real_stack.py", "test_a_reused_order_id_with_a_different_payload_sends_nothing_and_is_not_resumed")],
    "restart during submission": [
        ("test_open_crash_real_stack.py", "test_death_after_the_venue_filled_halts_the_restart_rather_than_starting_clean"),
        ("test_open_crash_real_stack.py", "test_death_between_the_intent_and_the_submit_marker_proves_never_sent_at_once"),
        ("test_open_crash_real_stack.py", "test_death_after_the_submit_marker_but_before_the_request_is_proven_never_sent_later")],
    "partial fills": [
        ("test_close_execution_faults.py", "test_a_partial_fill_then_cancel_is_finished_by_a_second_order_for_the_residual"),
        ("test_close_execution_faults.py", "test_partial_fills_that_keep_coming_up_short_end_in_a_halt_with_the_residual_on_file")],
    "late fills": [("test_close_plan.py", "test_a_late_fill_is_waited_for_not_doubled")],
    "cancel after fill": [("test_cancel_books_the_increment.py", "test_a_cancel_after_a_fully_resting_order_books_the_whole_fill")],
    "duplicate execution ids": [("test_close_plan.py", "test_duplicate_execution_events_add_nothing")],
    "order / execution disagreement": [("test_close_execution_faults.py", "test_executions_that_disagree_with_the_order_row_are_not_a_fill")],
    "unknown order status": [("test_carry_order_status_fail_closed.py", "test_an_unknown_status_is_never_crossed_on_top_of")],
    "position drift": [("test_carry_tick_safety.py", "test_a_venue_position_that_differs_from_the_book_halts"),
                       ("test_carry_tick_safety.py", "test_an_unreadable_venue_position_halts")],
    "manual intervention / flat that is not ours": [
        ("test_close_execution_faults.py", "test_flat_after_only_a_sliver_of_our_fills_is_not_called_our_close"),
        ("test_close_execution_faults.py", "test_the_boundary_at_production_size_one_lot_of_two_is_not_a_close"),
        ("test_close_execution_faults.py", "test_flat_with_none_of_our_fills_is_reported_unexplained_and_sends_nothing")],
    "one-leg hedge failure": [("test_carry_acquire_unwind_net_of_fee.py", "test_a_failed_perp_leg_sells_the_naked_spot_net_of_the_fee_too")],
    "failed reduce-only close": [
        ("test_close_execution_faults.py", "test_a_rejected_close_is_resolved_and_the_residual_is_never_dropped"),
        ("test_close_execution_faults.py", "test_a_refused_close_halts_and_leaves_the_short_on_file_not_abandoned")],
    "close accepted, acknowledgement lost": [("test_close_execution_faults.py", "test_a_lost_acknowledgement_is_found_by_lookup_and_nothing_is_sent_twice")],
    "unbooked fill blocks new risk": [("test_close_engine_integration.py", "test_a_fill_found_only_by_reading_the_venue_halts_unbooked_and_keeps_the_position")],
    "unreadable kill switch refuses entries": [("test_paper_readiness.py", "test_an_unreadable_kill_switch_blocks_too"),
                                               ("test_ops_forward_alerts.py", "test_an_unreadable_state_db_does_not_read_as_a_clear_kill_switch")],
    "unwind not confirmed by the venue is not flat": [
        ("test_unwind_completeness.py", "test_an_unreadable_venue_position_after_the_close_is_unknown_not_flat")],
}


def _functions(filename):
    tree = ast.parse(open(os.path.join(HERE, filename), encoding="utf-8").read())
    return {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


@pytest.mark.parametrize("scenario", sorted(MATRIX))
def test_every_required_lifecycle_scenario_still_has_its_named_tests(scenario):
    for filename, name in MATRIX[scenario]:
        assert os.path.exists(os.path.join(HERE, filename)), f"{scenario}: {filename} is gone"
        assert name in _functions(filename), f"{scenario}: {filename}::{name} was renamed or deleted"


MONEY_PATH = ("carry_engine.py", "carry_broker.py", "carry_risk.py", "carry_capital.py", "close_executor.py",
              "close_plan.py", "close_policy.py", "order_recovery.py", "order_lifecycle.py", "funding_reconcile.py",
              "ledger.py", "persistence.py", "portfolio_state.py", "bybit_connection.py", "venues.py", "promotion_gate.py")
ANALYTICS = {"md_capacity", "md_economics", "md_feed", "md_store", "md_engine", "md_state", "md_book", "md_rest",
             "md_transport", "md_wire", "md_events", "md_broker", "market_recorder", "evidence_chain", "evidence_stats",
             "capacity_schema", "funding_eval", "recorder_reliability", "trade_economics", "opportunity_engine",
             "profitability_report", "runner_supervisor", "capacity_summary", "state_backup"}


@pytest.mark.parametrize("module", MONEY_PATH)
def test_the_money_path_imports_no_recorder_or_analytics_module(module):
    tree = ast.parse(open(os.path.join(BOT, module), encoding="utf-8").read())
    imported = set()
    for n in ast.walk(tree):                         # includes function-level imports
        if isinstance(n, ast.Import):
            imported |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            imported.add((n.module or "").split(".")[0])
    assert not (imported & ANALYTICS), f"{module} imports {sorted(imported & ANALYTICS)}"


NEW_ANALYTICS = {"evidence_chain", "evidence_stats", "capacity_schema", "funding_eval", "recorder_reliability",
                 "trade_economics", "opportunity_engine", "profitability_report", "state_backup", "venue_qualification"}


def _imports_of(path):
    tree = ast.parse(open(path, encoding="utf-8").read())
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            out.add((n.module or "").split(".")[0])
    return out


def test_no_production_module_at_all_imports_the_evidence_economics_or_opportunity_modules():
    """The fixed MONEY_PATH list above misses main.py, config.py, trading_engine.py, the cost gates and the shadow
    modules; the review showed an `import opportunity_engine` there would have passed. So scan EVERY bot/*.py: the new
    analytics may be imported only by each other (and by tools/ and tests/, which are not in this glob)."""
    offenders = {}
    for name in sorted(os.listdir(BOT)):
        if not name.endswith(".py") or name[:-3] in NEW_ANALYTICS:
            continue
        bad = _imports_of(os.path.join(BOT, name)) & NEW_ANALYTICS
        if bad:
            offenders[name] = sorted(bad)
    assert not offenders, offenders


def test_the_guard_itself_can_fail(tmp_path):
    """Control: the scan really detects an import (a guard that cannot fail is not a guard)."""
    f = tmp_path / "carry_engine_like.py"
    f.write_text("def f():\n    import opportunity_engine\n")
    assert _imports_of(str(f)) & NEW_ANALYTICS == {"opportunity_engine"}


def test_the_only_place_the_trading_process_starts_the_feed_is_main_and_it_is_optional():
    src = open(os.path.join(BOT, "main.py"), encoding="utf-8").read()
    assert "MARKET_FEED_ENABLED" in src and "def _start_market_feed" in src
    # a feed that fails to start never raises into the trading loop (it degrades, entries stop)
    assert "market feed failed to start" in src
