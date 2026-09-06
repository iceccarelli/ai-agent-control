"""The forward-compatible observation record, the decay log, and the locked door.

WHAT THIS FILE IS GUARDING
==========================
Three things, in descending order of how badly they could go wrong:

1. **that history is not relabelled as experience.** The observation record is
   computed over the same data the cleared measurement used. If it ever claims
   to be a forward observation, or if the promotion gate ever counts it as one,
   the gate has been defeated by the one substitution it exists to prevent;
2. **that the shadow still pilots the cleared rule.** Slice 58 caught a wiring
   that entered mid-run — 55 trades at −0.0464 where the rule schedules 41 at
   +0.1736. Schedule drift is the most dangerous defect available here because
   the resulting monitors look perfectly healthy;
3. **that the gate refuses**, including against its own file.
"""
from __future__ import annotations

import ast
import inspect
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools"))

import project_status as ps  # noqa: E402
import promotion_gate as pg  # noqa: E402
import shadow  # noqa: E402
from signals import funding_carry_fade_btc_v1 as fb  # noqa: E402

ARTIFACTS = os.path.join(REPO, "artifacts")
FORWARD = os.path.join(ARTIFACTS, "slice59_forward_shadow.json")
GATE = os.path.join(ARTIFACTS, "slice59_promotion_gate.json")


def forward():
    with open(FORWARD, encoding="utf-8") as handle:
        return json.load(handle)


def gate_file():
    with open(GATE, encoding="utf-8") as handle:
        return json.load(handle)


# ===========================================================================
# 1 — the honesty block
# ===========================================================================


class TestTheObservationDoesNotClaimToBeForward:
    """The single most important group in this file.

    There is no data after the cleared measurement. A record that said
    otherwise would defeat the promotion gate's central item without anyone
    editing the gate.
    """

    def test_it_declares_itself_not_forward(self):
        payload = forward()
        assert payload["is_forward_observation"] is False
        assert payload["forward_observations_to_date"] == 0

    def test_it_says_why_in_its_own_fields(self):
        why = forward()["why_this_is_not_forward_evidence"]
        assert "ZERO bars have arrived" in why
        assert "not experience" in why

    def test_no_future_bars_were_invented(self):
        window = forward()["window"]
        assert window["invented_future_bars"] == 0
        assert window["bars_after_the_cleared_measurement"] == 0

    def test_the_window_ends_where_the_corpus_ends(self):
        """A record whose window ran past the data would be fabricating."""
        payload = forward()
        assert payload["window"]["corpus_last_bar_utc"][:10] == \
            payload["window"]["t1"][:10]

    def test_it_is_not_stage1_evidence_and_not_registration_eligible(self):
        payload = forward()
        assert payload["is_stage1_evidence"] is False
        assert payload["registration_eligible"] is False

    def test_the_gate_counts_zero_forward_observations(self):
        """The link between the two artefacts. If this ever disagrees, one of
        them is lying about the same fact."""
        item = gate_file()["checklist"]["forward_shadow_clean"]
        assert item["forward_trades_to_date"] == 0
        assert item["forward_days_to_date"] == 0
        assert item["complete"] is False
        assert forward()["forward_observations_to_date"] == \
            item["forward_trades_to_date"]

    def test_the_gate_says_a_replay_can_never_satisfy_that_item(self):
        item = gate_file()["checklist"]["forward_shadow_clean"]
        assert "does NOT count" in item["how_to_satisfy"]


# ===========================================================================
# 2 — the shadow still pilots the cleared rule
# ===========================================================================


class TestTheScheduleDidNotDrift:

    def test_the_record_declares_one_entry_per_run(self):
        assert forward()["schedule_mode"] == "one_entry_per_contiguous_run"

    def test_the_candidate_count_is_the_cleared_rules(self):
        """41 — the same number the Stage-1 run scheduled on this window.

        This is the assertion that catches drift back to 'every flagged bar',
        which produced 55.
        """
        payload = forward()
        assert payload["setups_or_candidates"] == 41
        assert payload["entries_taken"] + payload["entries_refused_by_caps"] \
            == payload["setups_or_candidates"]

    def test_the_taken_count_is_never_above_the_candidates(self):
        payload = forward()
        assert payload["entries_taken"] <= payload["setups_or_candidates"]

    def test_the_tool_uses_the_shared_schedule_builder(self):
        """AST: the candidate list must come from `simulate_schedule`, not from
        a bar-by-bar loop somebody wrote again."""
        import forward_shadow_observation as tool
        tree = ast.parse(inspect.getsource(tool))
        called = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                called.add(func.attr if isinstance(func, ast.Attribute)
                           else func.id if isinstance(func, ast.Name) else "")
        assert "simulate_schedule" in called
        assert "tradable_flags" in called

    def test_the_record_explains_the_slice58_defect(self):
        note = forward()["schedule_note"]
        assert "55 trades" in note and "41" in note

    def test_the_strategy_still_refuses_mid_run(self):
        """The causal form of the same rule, still in place."""
        import shadow_strategy
        source = inspect.getsource(shadow_strategy.ShadowStrategy._current_setup)
        assert "decision_index - 1" in source


class TestTheConstantsAndCapsAreUnchanged:

    def test_the_fingerprint_equals_slice_58(self):
        payload = forward()
        assert payload["constants_fingerprint"] == \
            shadow.CONSTANTS_FINGERPRINT == \
            shadow.constants_fingerprint()[:32]
        assert payload["constants_fingerprint_matches_slice58"] is True

    def test_the_constants_are_the_frozen_ones(self):
        assert forward()["constants"] == dict(fb.CONSTANTS) == {
            "FUND_ABS": 0.0001, "STOP_ATR": 1.5, "TAKE_PROFIT_R": 1.0,
            "TAKE_PROFIT_ATR": 1.5, "HORIZON": 5, "ATR_PERIOD": 14,
            "LOCKUP": 1, "ROUND_TRIP_BPS": 25.0, "ENTRY_ON": "next_open"}

    def test_the_caps_are_still_one_one_and_a_hundred(self):
        caps = forward()["caps"]
        assert caps["max_concurrent_positions"] == 1
        assert caps["max_entries_per_day"] == 1
        assert caps["max_notional_usd"] == 100.00
        assert caps["symbol"] == "BTCUSDT"

    def test_the_notional_was_not_raised(self):
        """The mission forbids exceeding 100. Asserted from three places so a
        single edit cannot pass."""
        assert shadow.SHADOW_MAX_NOTIONAL_USD <= 100.00
        assert forward()["caps"]["max_notional_usd"] <= 100.00
        assert gate_file()["max_notional_usd"] <= 100.00

    def test_the_tool_exposes_no_flag_that_could_change_a_number(self):
        """A replay tool with a --fund-abs flag is a retuning tool with a
        monitoring label."""
        import forward_shadow_observation as tool
        source = inspect.getsource(tool)
        for flag in ("--fund-abs", "--stop-atr", "--take-profit",
                     "--horizon", "--round-trip-bps", "--max-notional",
                     "--threshold"):
            assert flag not in source, flag


# ===========================================================================
# 3 — the decay log, unmoved
# ===========================================================================


class TestTheMonitorsWereNotMoved:

    SLICE58 = {
        "M1_rolling_trades": {"k": 10, "warn_below": 0.0, "alert_below": -0.25,
                              "min_trades": 10},
        "M2_rolling_days": {"window_days": 90, "warn_below": 0.0,
                            "alert_below": -0.25, "min_trades": 5},
        "M3_concentration": {"top_n": 3, "warn_above": 0.60, "min_trades": 10},
        "M4_halves": {"min_trades": 20},
    }

    def test_the_live_thresholds_are_slice_58s(self):
        assert shadow.MONITOR_THRESHOLDS == self.SLICE58

    def test_the_record_carries_the_same_thresholds(self):
        assert forward()["monitor_thresholds"] == self.SLICE58

    def test_the_record_says_no_threshold_moved(self):
        assert forward()["thresholds_moved_this_slice"] is False

    def test_the_slice58_pack_agrees(self):
        """Cross-artefact: if slice 58's pack and this record disagree about a
        threshold, one of them was edited."""
        with open(os.path.join(ARTIFACTS, "slice58_shadow_pack.json"),
                  encoding="utf-8") as handle:
            pack = json.load(handle)
        assert pack["monitors"] == self.SLICE58

    def test_all_four_monitors_are_reported(self):
        names = [r["name"] for r in forward()["monitor_readings"]]
        assert names == ["M1_rolling_trades", "M2_rolling_days",
                         "M3_concentration", "M4_halves"]

    def test_every_reading_carries_its_state_and_count(self):
        for reading in forward()["monitor_readings"]:
            assert reading["state"] in {"OK", "WARN", "ALERT",
                                        "INSUFFICIENT_DATA"}
            assert isinstance(reading["n_trades"], int)


class TestM4IsRecordedAndNotSilenced:
    """§42d: M-4 is expected history, not a defect to erase.

    This is the test a future slice would have to delete in order to make the
    WARN go away quietly. Deleting it is a visible act.
    """

    def test_m4_still_warns(self):
        m4 = next(r for r in forward()["monitor_readings"]
                  if r["name"] == "M4_halves")
        assert m4["state"] == "WARN"

    def test_the_aggregate_status_reports_it(self):
        assert forward()["monitor_status"] == "WARN"

    def test_the_record_carries_a_note_that_does_not_soften_it(self):
        note = forward()["m4_recent_half_note"]
        assert "WARN" in note
        assert "NOT a defect to erase" in note
        assert "AS IT STANDS" in note      # i.e. the threshold does not move

    def test_a_warn_does_not_block_entries(self):
        """By design — only ALERT blocks. A WARN that blocked would make the
        monitor a trading rule."""
        assert forward()["entries_blocked_by_monitor"] is False

    def test_the_gate_carries_m4_as_an_incomplete_item(self):
        item = gate_file()["checklist"]["m4_recent_half_accepted_or_recovered"]
        assert item["complete"] is False
        assert "WARN" in item["evidence"]

    def test_the_only_legitimate_recovery_is_new_data(self):
        item = gate_file()["checklist"]["m4_recent_half_accepted_or_recovered"]
        assert "UNCHANGED THRESHOLD" in item["how_to_satisfy"].upper()
        assert "forbidden" in item["how_to_satisfy"]


# ===========================================================================
# 4 — the record cannot register anything
# ===========================================================================


class TestTheObservationCannotRegister:

    def test_it_is_not_a_summary_file(self):
        """Structural, not a policy: the hook only reads `*_summary.json`."""
        assert not FORWARD.endswith("_summary.json")

    def test_it_carries_none_of_the_gate_fields(self):
        payload = forward()
        for key in ("verdict", "m1", "m2", "observed", "oos_folds_sha256"):
            assert key not in payload, key

    def test_dropping_it_into_an_artefact_directory_registers_nothing(
            self, tmp_path):
        (tmp_path / "slice59_forward_shadow.json").write_text(
            json.dumps(forward()), encoding="utf-8")
        assert ps.cleared_edge_signal_from_artifacts(str(tmp_path)) is None

    def test_even_renamed_as_a_summary_it_registers_nothing(self, tmp_path):
        """The obvious attack: rename it and hope."""
        (tmp_path / "x_summary.json").write_text(
            json.dumps(forward()), encoding="utf-8")
        assert ps.cleared_edge_signal_from_artifacts(str(tmp_path)) is None

    def test_the_real_clear_still_comes_from_the_slice57_artefact(self):
        assert ps.cleared_edge_signal_from_artifacts(ARTIFACTS) == \
            "funding_carry_fade_btc_v1"
        with open(os.path.join(
                ARTIFACTS, "slice57_oos_edge_BTCUSDT_summary.json"),
                encoding="utf-8") as handle:
            oos = json.load(handle)
        assert oos["observed"]["n_trades"] == 41
        assert round(oos["m1"]["percentile"], 2) == 95.13
        assert oos["m2"]["percentile"] == 96.0
        assert oos["control_validated"] is True

    def test_the_clear_was_not_re_scored_this_slice(self):
        assert forward()["cleared_edge_re_scored_this_slice"] is False
        offending = [n for n in os.listdir(ARTIFACTS)
                     if n.startswith("slice59_")
                     and ("edge_" in n or "control_" in n or "_summary" in n)]
        assert not offending, offending


# ===========================================================================
# 5 — the gate refuses
# ===========================================================================


class TestThePromotionGateRefuses:

    def test_it_refuses_on_this_tree(self):
        assert pg.promotion_gate_allows_live() is False

    def test_six_of_eight_items_are_incomplete(self):
        verdict = pg.evaluate_promotion_gate()
        assert len(verdict.items) == 8
        assert len(verdict.incomplete) == 6

    def test_every_reason_is_stated(self):
        verdict = pg.evaluate_promotion_gate()
        assert verdict.reasons
        for key in verdict.incomplete:
            assert any(key in reason for reason in verdict.reasons), key

    def test_most_items_need_a_human(self):
        """A gate a process could satisfy on its own is not a gate."""
        verdict = pg.evaluate_promotion_gate()
        human = [i for i in verdict.items if i.owner != "machine"]
        assert len(human) == 6

    def test_the_minimums_match_the_design_note(self):
        assert pg.MIN_FORWARD_TRADES == 20
        assert pg.MIN_FORWARD_DAYS == 180
        assert gate_file()["min_forward_trades"] == 20
        assert gate_file()["min_forward_days"] == 180

    def test_the_minimum_is_the_count_at_which_the_last_monitor_arms(self):
        """§42e's derivation, asserted rather than trusted."""
        assert pg.MIN_FORWARD_TRADES >= \
            shadow.MONITOR_THRESHOLDS["M4_halves"]["min_trades"]

    def test_the_minimum_days_span_two_m2_windows(self):
        assert pg.MIN_FORWARD_DAYS >= \
            2 * shadow.MONITOR_THRESHOLDS["M2_rolling_days"]["window_days"]

    # -- fail closed ------------------------------------------------------

    def test_a_missing_gate_file_refuses(self, tmp_path):
        assert pg.promotion_gate_allows_live(
            path=str(tmp_path / "absent.json")) is False

    def test_an_unreadable_gate_file_refuses(self, tmp_path):
        path = tmp_path / "gate.json"
        path.write_text("{ not json", encoding="utf-8")
        assert pg.promotion_gate_allows_live(path=str(path)) is False

    def test_a_gate_file_with_no_checklist_refuses(self, tmp_path):
        path = tmp_path / "gate.json"
        path.write_text(json.dumps({"schema": "promotion_gate/1"}),
                        encoding="utf-8")
        assert pg.promotion_gate_allows_live(path=str(path)) is False

    def test_flipping_every_bool_still_refuses(self, tmp_path):
        """THE test. Someone edits the JSON to say everything is done."""
        payload = gate_file()
        for entry in payload["checklist"].values():
            entry["complete"] = True
        path = tmp_path / "gate.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        assert pg.promotion_gate_allows_live(path=str(path)) is False

    def test_a_malformed_live_chain_answer_refuses(self, monkeypatch,
                                                   tmp_path):
        """The bug this file shipped with, pinned so it cannot return.

        `config.is_live_authorized` returns `(live_allowed, reason)`. The first
        version of the gate assumed a 3-tuple and read `result[1]` — the REASON
        STRING — which is truthy, so the gate reported the live chain satisfied
        while the shell sat in SANDBOX. With the checklist flipped it said YES.

        Anything that is not exactly a `(bool, str)` pair must now refuse.
        """
        import config as _config
        payload = gate_file()
        for entry in payload["checklist"].values():
            entry["complete"] = True
        path = tmp_path / "gate.json"
        path.write_text(json.dumps(payload), encoding="utf-8")

        for bad in ((False, True, "reason"), ("yes", "reason"), True,
                    (None, "reason"), ()):
            monkeypatch.setattr(_config, "is_live_authorized",
                                lambda *a, **k: bad)
            assert pg.promotion_gate_allows_live(path=str(path)) is False, bad

    def test_the_live_chain_is_read_correctly_when_it_says_yes(
            self, monkeypatch, tmp_path):
        """The control: with a well-formed affirmative AND a complete
        checklist, the gate does allow. Without this, 'always refuses' could
        mean 'is broken', and every refusal above would prove nothing."""
        import config as _config
        payload = gate_file()
        for entry in payload["checklist"].values():
            entry["complete"] = True
        path = tmp_path / "gate.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setattr(_config, "is_live_authorized",
                            lambda *a, **k: (True, "armed by a human"))
        assert pg.promotion_gate_allows_live(path=str(path)) is True

    def test_but_on_the_real_tree_it_still_refuses(self):
        """And the real config is not that. Belt to the braces."""
        import config as _config
        allowed, _reason = _config.is_live_authorized(_config.load({}))
        assert allowed is False
        assert pg.promotion_gate_allows_live() is False

    def test_and_the_reason_is_the_live_chain(self, tmp_path):
        payload = gate_file()
        for entry in payload["checklist"].values():
            entry["complete"] = True
        path = tmp_path / "gate.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        verdict = pg.evaluate_promotion_gate(path=str(path))
        assert any("live-arming chain" in reason for reason in verdict.reasons)

    def test_a_forged_notional_in_the_file_does_not_satisfy_the_cap_item(
            self, tmp_path):
        """The machine items are re-derived from the live tree, not read."""
        payload = gate_file()
        payload["max_notional_usd"] = 1_000_000.0
        payload["checklist"]["notional_cap_within_policy"]["complete"] = True
        path = tmp_path / "gate.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        verdict = pg.evaluate_promotion_gate(path=str(path))
        item = next(i for i in verdict.items
                    if i.key == "notional_cap_within_policy")
        assert item.complete is False

    def test_a_forged_model_claim_does_not_satisfy_the_model_item(
            self, tmp_path):
        payload = gate_file()
        payload["checklist"]["models_current_absent_or_contained"][
            "complete"] = True
        path = tmp_path / "gate.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        # Truthful today (models/current really is absent), so point the
        # evaluator at a tree where it is not.
        fake_repo = tmp_path / "repo"
        (fake_repo / "models").mkdir(parents=True)
        (fake_repo / "models" / "current").write_text("x", encoding="utf-8")
        verdict = pg.evaluate_promotion_gate(path=str(path),
                                             repo=str(fake_repo))
        item = next(i for i in verdict.items
                    if i.key == "models_current_absent_or_contained")
        assert item.complete is False
        assert verdict.allows_live is False


class TestTheGateCannotArmAnything:

    def test_no_module_assigns_live_authorized(self):
        """Including this one. Arming remains config.is_live_authorized's
        alone, and it is computed, not written."""
        for module in ("promotion_gate", "shadow", "shadow_strategy",
                       "risk_management", "trading_engine", "policy",
                       "position_sizing", "main"):
            tree = ast.parse(inspect.getsource(__import__(module)))
            for node in ast.walk(tree):
                targets = []
                if isinstance(node, ast.Assign):
                    targets = node.targets
                elif isinstance(node, ast.AugAssign):
                    targets = [node.target]
                for target in targets:
                    name = (target.attr if isinstance(target, ast.Attribute)
                            else target.id if isinstance(target, ast.Name)
                            else "")
                    assert name != "live_authorized", module

    def test_the_gate_places_no_orders_and_clears_no_switch(self):
        tree = ast.parse(inspect.getsource(pg))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                called = (func.attr if isinstance(func, ast.Attribute)
                          else func.id if isinstance(func, ast.Name) else "")
                assert "kill_switch" not in called
                assert "order" not in called.lower()

    def test_the_gate_writes_nothing(self):
        """A predicate that writes is not a predicate."""
        tree = ast.parse(inspect.getsource(pg))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id == "open":
                    mode = [a for a in node.args[1:2]]
                    if mode and isinstance(mode[0], ast.Constant):
                        assert "w" not in str(mode[0].value), ast.dump(node)
                    for kw in node.keywords:
                        if kw.arg == "mode" and isinstance(kw.value,
                                                           ast.Constant):
                            assert "w" not in str(kw.value.value)

    def test_the_kill_switch_is_still_human_clear_only(self):
        import persistence
        source = inspect.getsource(persistence)
        assert "clear_kill_switch_by_human" in source

    def test_live_is_still_dark(self):
        status = ps.current()
        assert status.live_authorized is False
        assert status.policy_mode == "off"
        assert status.execution_mode == "paper"
        assert status.models_current_present is False

    def test_the_doc_and_the_machine_state_agree(self):
        with open(os.path.join(REPO, "docs",
                               "PROMOTION_GATE_MICRO_LIVE.md"),
                  encoding="utf-8") as handle:
            doc = handle.read()
        verdict = pg.evaluate_promotion_gate()
        complete = sum(1 for i in verdict.items if i.complete)
        assert f"{complete} of {len(verdict.items)} items complete" in doc
        assert "REFUSE" in doc
        for key, _description, _owner in pg.CHECKLIST_ITEMS:
            assert key in json.dumps(gate_file()), key


# ===========================================================================
# 6 — the standing posture, unchanged
# ===========================================================================


class TestNothingElseMoved:

    def test_the_eleven_freezes_are_intact(self):
        assert len(ps.ABSENT_SIGNALS) == 11
        assert "funding_carry_fade_v1" in ps.ABSENT_SIGNALS
        assert ps.FROZEN_STATUS["funding_carry_fade_v1"] == "ABSENT"

    @pytest.mark.parametrize("signal", list(ps.ABSENT_SIGNALS))
    def test_a_perfect_forgery_for_each_frozen_name_is_refused(self, signal,
                                                               tmp_path):
        summary = {
            "symbol": "BTCUSDT", "signal": signal,
            "observed": {"n_trades": 500, "mean_r": 1.0},
            "m1": {"percentile": 99.9, "bar": 95.0, "passed": True},
            "m2": {"percentile": 99.9, "bar": 95.0, "passed": True},
            "control_validated": True, "verdict": "EDGE_EVIDENCE_POSITIVE",
            "registration_eligible": True, "window": "oos_late",
        }
        (tmp_path / "x_summary.json").write_text(json.dumps(summary),
                                                 encoding="utf-8")
        assert ps.cleared_edge_signal_from_artifacts(str(tmp_path)) is None

    def test_eth_and_sol_were_not_measured_under_the_cleared_name(self):
        offending = [n for n in os.listdir(ARTIFACTS)
                     if "funding_carry_fade_btc" in n
                     and ("ETH" in n or "SOL" in n)]
        assert not offending, offending

    def test_the_record_does_not_claim_progress_toward_autonomy(self):
        assert forward()["closer_to_autonomous_profit_agent"] is False

    def test_the_universe_is_still_one_symbol(self):
        assert fb.UNIVERSE == ("BTCUSDT",)
        with pytest.raises(fb.UnsupportedSymbol):
            fb.require_supported_symbol("ETHUSDT")
