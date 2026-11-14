"""Round 22 (Phase 11, red team the money path): every failure mode the owner named has a DETERMINISTIC SAFE OUTCOME, stated once, with the tests that exercise it.

This file is the register. It does not re-test anything: it fails if a mode loses its statement, its safe outcome or the tests that back the claim (a referenced test that no longer exists is a lie about
coverage), so the matrix cannot quietly rot. ``OWNER`` marks what no repository test can prove (a live machine, the real venue); ``OPEN`` marks an honest gap with its reason: none is hidden.

A mode counts as COVERED only when its safe outcome is exercised by a named, existing test in this repository. Nothing here is evidence about a real venue."""
from __future__ import annotations

import ast
import glob
import os

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))

#: mode -> (safe outcome, [test references "file::function"], status, note)
MATRIX = {
    "stale market data": ("no new exposure: a stale book or mark refuses the entry before any order", ["test_ms_edge_gate.py::test_a_book_from_beyond_the_skew_bound_is_still_refused", "test_ms_portfolio.py::test_drawdown_stale_account_and_stale_market_halt_new_exposure"], "COVERED", ""),
    "missing funding": ("unknown, never zero: a missing funding record is UNKNOWN in the books and keeps the net UNKNOWN; a funding read that fails is WAIT_FOR_DATA, never a session", ["test_ms_accounting.py::test_missing_funding_record_is_unknown_not_zero"], "COVERED", ""),
    "missing fees": ("unknown, never zero: a missing fee is UNKNOWN and the gate cannot call the cost known", ["test_exec_quality.py::test_a_missing_fee_is_unknown_not_zero", "test_eligibility_gate.py::test_an_unknown_cost_line_is_unknown_not_zero_and_blocks_trading"], "COVERED", ""),
    "incorrect fees": ("a fee in an unexpected currency ends flat and publishes no fee total; the mechanics verifier fails a session whose fee total disagrees with its executions", ["test_ms_testnet_executor.py::test_a_session_whose_entry_fee_is_in_a_third_currency_ends_flat_and_publishes_no_fee_total", "test_mechanics_verify.py::test_each_tampered_session_record_is_caught_by_the_check_that_owns_the_fact"], "COVERED", ""),
    "incorrect leverage": ("a nonsense leverage is refused at construction; the plan is built at leverage 1 and the venue's own leverage is not trusted", ["test_pretrade_capital_check.py::test_a_nonsense_leverage_is_refused_at_construction"], "COVERED", "the live leverage setting on the venue is read at preflight; a hedge-mode account is refused"),
    "partial fills": ("the entry unwinds to flat in the session and in one owner unwind; a partial exit does not halt the group with an open hedge", ["test_execution_known_gaps.py::test_R2_a_partial_perp_entry_fill_must_unwind_to_flat_in_the_session_and_in_ONE_owner_unwind"], "COVERED", ""),
    "duplicate fills": ("a duplicate venue execution event is one execution (never double counted in quantity, fees or P&L)", ["test_exec_quality.py::test_a_duplicate_venue_execution_event_is_one_execution"], "COVERED", ""),
    "rejected orders": ("a venue rejection of an exit counts toward the exit halt; two failed creates of the second leg never leave a naked perp", ["test_ms_execution_gaps.py::test_a_held_spot_exit_never_counts_toward_the_exit_halt_but_a_venue_rejection_does", "test_execution_known_gaps.py::test_R6_two_failed_creates_of_the_second_leg_must_not_leave_a_naked_perp_short"], "COVERED", ""),
    "delayed orders": ("a session slowed past the settlement margin sends nothing and is a SKIP with its reason; a delayed fill after a cancel reopens the group as unhedged and it is unwound", ["test_ms_testnet_autopilot.py::test_a_session_slowed_past_the_margin_sends_nothing_and_is_a_skip_with_its_reason", "test_ms_execution.py::test_delayed_fill_after_cancel_reopens_the_group_as_unhedged_and_it_is_unwound"], "COVERED", ""),
    "cancelled orders": ("a cancel that races a fill leaves the leg UNKNOWN until reconcile adopts the fill exactly once; a lost cancel ack is resolved by reconcile", ["test_ms_execution.py::test_cancel_race_the_order_filled_before_the_cancel_so_the_leg_is_unknown_until_reconcile_adopts_the_fill_once", "test_ms_execution.py::test_a_lost_cancel_ack_leaves_the_leg_unknown_and_reconcile_resolves_it"], "COVERED", ""),
    "websocket disconnect": ("the recorder is restarted with backoff and never disturbs the monitor; a fresh heartbeat with no growth or dead feed threads is STALLED, never healthy", ["test_runner_supervisor.py::test_a_dying_recorder_is_restarted_and_never_disturbs_the_monitor", "test_recorder_health.py::test_a_fresh_heartbeat_with_no_growth_is_stalled_and_dead_feed_threads_are_stalled"], "COVERED", "the executor does not use a websocket for order decisions: it reads REST"),
    "REST failure": ("an unreadable venue pauses the loop without an alert and retries; the flat proof is never inferred from a failed read", ["test_ms_testnet_autopilot.py::test_an_unreadable_venue_pauses_without_an_alert_and_is_retried", "test_ms_testnet_autopilot.py::test_recovery_refuses_an_unresolved_session_whose_spot_baseline_is_unknown"], "COVERED", ""),
    "stale private state": ("stale account data refuses new exposure and nothing reaches the venue", ["test_ms_testnet_executor.py::test_stale_account_data_refuses_new_exposure_and_nothing_reaches_the_venue"], "COVERED", ""),
    "local DB corruption": ("a corrupt capital book stops the session before any order and is never reset; the view is not trusted over the lineage", ["test_ms_testnet_autopilot.py::test_a_corrupt_capital_book_stops_the_session_before_any_order_and_is_not_reset"], "COVERED", "the recorder's SQLite segments verify by hash chain (md_store tests)"),
    "process restart": ("an exception out of a session forces a fresh venue proof before anything else and never kills the loop; a restart between park and settle loses and duplicates nothing", ["test_ms_testnet_autopilot.py::test_RT2c_an_exception_out_of_a_session_forces_a_fresh_venue_proof_and_never_kills_the_loop", "test_ms_capital.py::test_a_restart_between_park_and_settle_loses_nothing_and_duplicates_nothing"], "COVERED", ""),
    "machine restart": ("a restart is not permission to trade: recovery proves the account flat from the venue for every allowlisted symbol, and an unresolved session with an unknown baseline is refused", ["test_ms_testnet_autopilot.py::test_a_flat_venue_after_an_unresolved_prior_session_is_recorded_and_allowed", "test_ms_testnet_autopilot.py::test_recovery_refuses_an_unresolved_session_whose_spot_baseline_is_unknown"], "COVERED", ""),
    "duplicate executor": ("the Supabase single-writer lease keeps a second executor paused", ["test_ms_testnet_autopilot.py::test_a_lease_held_by_another_executor_keeps_this_one_paused"], "COVERED", "the lease itself is proven on a real PostgreSQL in CI; a live two-machine race is OWNER-side"),
    "lease loss": ("a lease lost between the gates and the first order places no entry", ["test_ms_testnet_autopilot.py::test_RT1_a_lease_lost_between_the_gates_and_the_first_order_places_no_entry"], "COVERED", ""),
    "clock skew": ("a preflight with a clock skew beyond the limit is refused; a book beyond the skew bound is refused", ["test_ms_testnet_session.py::test_preflight_refuses_a_clock_skew_beyond_the_limit", "test_ms_edge_gate.py::test_a_book_from_beyond_the_skew_bound_is_still_refused"], "COVERED", ""),
    "capital mismatch": ("cash the ledger cannot explain stops new entries until the owner has looked; a loss bigger than the ledger knew is still debited in full", ["test_ms_capital.py::test_a_cash_loss_bigger_than_the_ledger_knew_is_still_debited_in_full"], "COVERED", ""),
    "position mismatch": ("a position mismatch between the engine and the venue halts new exposure", ["test_ms_execution.py::test_position_mismatch_between_engine_and_venue_halts_new_exposure"], "COVERED", ""),
    "hedge mismatch": ("a naked perp is never left; the mechanics verifier fails a session whose perp and spot fills do not match", ["test_execution_known_gaps.py::test_R6_two_failed_creates_of_the_second_leg_must_not_leave_a_naked_perp_short", "test_mechanics_verify.py::test_each_tampered_session_record_is_caught_by_the_check_that_owns_the_fact"], "COVERED", ""),
    "close failure": ("a group halted by real exit rejections is unwound by the confirmed owner command; the session is NOT_FLAT and alerts, never DONE", ["test_executor_lock.py::test_a_group_halted_by_REAL_exit_rejections_is_unwound_by_the_confirmed_owner_command"], "COVERED", ""),
    "venue/local reconciliation mismatch": ("a disagreement between the venue read and the books halts new exposure and is reported", ["test_ms_pipeline_cli.py::test_a_disagreement_between_the_venue_read_and_the_books_halts_new_exposure_and_is_reported"], "COVERED", ""),
    "missing evidence": ("a missing or malformed session file makes the mechanics verdict INCONCLUSIVE, never VERIFIED; the verifier never crashes", ["test_mechanics_verify.py::test_a_missing_or_malformed_file_is_inconclusive_not_a_crash_and_never_verified"], "COVERED", ""),
    "recorder stall": ("a stalled recorder is stalled/unknown in recorder_health (never healthy) and alerts; rows copied from a dead recorder's status are not read as progress", ["test_recorder_health.py::test_a_fresh_heartbeat_with_no_growth_is_stalled_and_dead_feed_threads_are_stalled", "test_recorder_forensics.py::test_rows_copied_from_a_stale_status_file_are_not_observed_progress_or_the_lack_of_it"], "COVERED", ""),
    "storage exhaustion": ("the executor refuses a session below its free-space floor and survives a full state volume; the recorder reports storage_blocked and warns before a resumed write would prune retained days", ["test_ms_testnet_autopilot.py::test_a_full_state_volume_does_not_crash_the_loop_and_no_session_starts_while_it_stays_full", "test_ms_testnet_autopilot.py::test_a_low_state_volume_refuses_the_session_and_the_floor_can_only_be_raised", "test_recorder_health.py::test_a_stalled_store_warns_that_resuming_will_prune_the_retained_evidence_days"], "COVERED", "the live recorder volume is OWNER state"),
    "image mismatch": ("an unverified image is never healthy; the entrypoint refuses to start (exit 78) when provenance does not verify", ["test_recorder_health.py::test_an_unverified_image_is_image_mismatch_even_when_the_recorder_writes_fine", "test_runtime_provenance.py::test_the_dockerfile_build_step_would_produce_a_manifest_that_verifies_and_the_entrypoint_gates_on_it"], "COVERED", "what a machine actually runs is read by the OWNER (registry digest, in-machine verify)"),
    "bad environment": ("any LIVE_* / BYBIT_API_* variable or a falsy USE_TESTNET / PAPER_TRADING is refused before any read; the loop pauses with no alert marker", ["test_ms_testnet_autopilot.py::test_run_once_refuses_a_deployment_that_carries_an_arming_switch_before_any_read"], "COVERED", ""),
    "wrong account": ("a root that belongs to another account is refused; the capital lineage is bound to the venue's stated account", ["test_capital_anchor.py::test_another_account_is_refused"], "COVERED", ""),
    "wrong venue": ("the executor is Testnet-only whatever a forged capability says", ["test_runtime_capability.py::test_executor_never_mainnet_or_demo_even_if_the_host_matches_a_forged_capability"], "COVERED", ""),
    "wrong symbol": ("a symbol outside the capability allowlist is rejected by the risk gate", ["test_ms_risk.py::test_symbol_outside_allowlist_rejected"], "COVERED", ""),
    "unexpected exposure": ("a perp on ANY symbol or a foreign open order makes the account not flat; the session is not resolved", ["test_ms_testnet_session.py::test_a_perp_position_on_another_symbol_is_not_flat", "test_ms_testnet_executor.py::test_flat_verified_is_false_with_a_foreign_open_order_on_the_symbol"], "PARTIAL", "a perp on ANY symbol is now covered; but the session's own open-order read is still per executor symbol and ordinary filter only, so a conditional (stop) order or a foreign order on another symbol is invisible to the session's own flat claim: `venue-snapshot` reads them account-wide and `mechanics_verify` requires that snapshot before it says MECHANICS_VERIFIED. Closing it inside the session's own proof is a remaining item"),
}
#: the directive's list, verbatim: the matrix must name every one
DIRECTIVE = ("stale market data", "missing funding", "missing fees", "incorrect fees", "incorrect leverage", "partial fills", "duplicate fills", "rejected orders", "delayed orders", "cancelled orders",
             "websocket disconnect", "REST failure", "stale private state", "local DB corruption", "process restart", "machine restart", "duplicate executor", "lease loss", "clock skew", "capital mismatch",
             "position mismatch", "hedge mismatch", "close failure", "venue/local reconciliation mismatch", "missing evidence", "recorder stall", "storage exhaustion", "image mismatch", "bad environment",
             "wrong account", "wrong venue", "wrong symbol", "unexpected exposure")


def _test_functions():
    out = {}
    for path in glob.glob(os.path.join(HERE, "test_*.py")):
        try:
            tree = ast.parse(open(path, encoding="utf-8").read())
        except (OSError, SyntaxError):
            continue
        out[os.path.basename(path)] = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    return out


def test_the_matrix_names_every_failure_mode_the_directive_lists_and_no_other_is_silently_dropped():
    assert set(MATRIX) == set(DIRECTIVE), (set(DIRECTIVE) - set(MATRIX), set(MATRIX) - set(DIRECTIVE))
    assert len(DIRECTIVE) == 33


@pytest.mark.parametrize("mode", DIRECTIVE)
def test_each_failure_mode_has_a_stated_safe_outcome_and_only_existing_tests_back_it(mode):
    safe, refs, status, note = MATRIX[mode]
    assert len(safe) > 30 and status in ("COVERED", "PARTIAL", "OWNER", "OPEN")
    if status in ("PARTIAL", "OPEN"):
        assert note, f"{mode}: an honest gap must say why"
    assert refs or status in ("OWNER", "OPEN")
    fns = _test_functions()
    for ref in refs:
        fname, func = ref.split("::")
        assert fname in fns, f"{mode}: test file {fname} does not exist"
        assert func in fns[fname], f"{mode}: {ref} does not exist (a coverage claim that points at nothing)"


def test_the_register_does_not_overstate_a_mode_that_depends_on_the_live_machine():
    for mode in ("duplicate executor", "image mismatch", "storage exhaustion"):
        assert MATRIX[mode][3], f"{mode}: what only the owner can prove must be said"
