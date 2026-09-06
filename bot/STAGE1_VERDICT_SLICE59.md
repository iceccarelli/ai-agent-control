# SLICE 59 — VERDICT

```
SLICE59_VERDICT:                    PASS
cleared_edge_signal:                funding_carry_fade_btc_v1
oos_evidence_re_scored:             NO
shadow_schedule_one_per_run:        YES   (41 candidates — the cleared rule's own)
caps_usd_100:                       YES   (100.00, unchanged)
monitor_thresholds_unchanged:       YES   (identical to slice 58, three ways)
monitor_status:                     WARN  (M4_halves)
m4_recent_half_recorded:            YES
promotion_gate_allows_live:         NO
live_authorized:                    false
models_current_present:             false
frozen_absent_count:                11
Closer to autonomous profit agent?: NO
```

## Evidence table

| claim | observed | file / command |
|---|---|---|
| baseline all-true before any change | 20/20 rows; 3,854 passed, 2 skipped | `artifacts/slice59_baseline_pytest.log` |
| design note precedes all code | EDGE §42 commit `--stat` lists `EDGE.md` + baseline log only; no `.py` | `git show --stat` |
| minimums declared before any new reading | 20 trades / 180 days, in §42e, committed first | `EDGE.md` §42e |
| clear intact, not re-scored | n=41, M1 95.13, M2 96.0, mean R +0.1736, control true | `artifacts/slice57_oos_edge_BTCUSDT_summary.json` |
| no re-score artefacts produced | no `slice59_*edge*`, `*control*` or `*_summary*` exists | `test_the_clear_was_not_re_scored_this_slice` |
| **the record does not claim to be forward** | `is_forward_observation: false`, `forward_observations_to_date: 0` | `artifacts/slice59_forward_shadow.json` |
| no future bars invented | `invented_future_bars: 0`; window ends where the corpus ends (2026-08-09) | `test_no_future_bars_were_invented` |
| the gate counts zero forward observations | `forward_shadow_clean`: 0/20, 0/180, incomplete | `artifacts/slice59_promotion_gate.json` |
| a replay can never satisfy that item | `how_to_satisfy` says a replay "does NOT count" | `test_the_gate_says_a_replay_can_never_satisfy_that_item` |
| **schedule is one-per-run** | 41 candidates = 35 taken + 6 refused by caps | `test_the_candidate_count_is_the_cleared_rules` |
| the tool uses the shared builder, not a loop | AST: `simulate_schedule` and `tradable_flags` both called | `test_the_tool_uses_the_shared_schedule_builder` |
| the strategy still refuses mid-run | `decision_index - 1` guard present | `test_the_strategy_still_refuses_mid_run` |
| constants unchanged | fingerprint `662de011…` in three places | `test_the_fingerprint_equals_slice_58` |
| notional not raised | ≤ 100.00 asserted from module, record and gate file | `test_the_notional_was_not_raised` |
| no tool flag could change a number | seven forbidden flags absent from the source | `test_the_tool_exposes_no_flag_that_could_change_a_number` |
| **thresholds unmoved** | live map == record == slice-58 pack | `TestTheMonitorsWereNotMoved` (6 tests) |
| **M-4 still WARN, recorded not silenced** | earlier +0.4669 on 17, recent −0.0798 on 18 | `TestM4IsRecordedAndNotSilenced` (6 tests) |
| a WARN does not block entries | `entries_blocked_by_monitor: false` | same |
| the record cannot register | not a summary; refused even when renamed `x_summary.json` | `test_even_renamed_as_a_summary_it_registers_nothing` |
| the clear still comes from slice 57 only | hook returns the name from the OOS artefact | `test_the_real_clear_still_comes_from_the_slice57_artefact` |
| **gate refuses** | `promotion_gate_allows_live() == False`; 6 of 8 incomplete | `TestThePromotionGateRefuses` |
| six of eight items need a human | owners counted | `test_most_items_need_a_human` |
| minimums match their derivations | 20 ≥ M-4's arming count; 180 ≥ 2× M-2's window | `test_the_minimum_is_the_count_at_which_the_last_monitor_arms` |
| missing / unreadable / empty gate file refuses | three cases | `TestThePromotionGateRefuses` |
| **flipping every bool still refuses** | live chain unsatisfied | `test_flipping_every_bool_still_refuses` |
| forged notional / model claims do not satisfy | machine items re-derived from the tree | `test_a_forged_notional_in_the_file_does_not_satisfy_the_cap_item` |
| no module assigns `live_authorized` | AST over 8 modules incl. the gate | `test_no_module_assigns_live_authorized` |
| the gate writes nothing and places no orders | AST | `TestTheGateCannotArmAnything` |
| kill switch still human-clear only | `clear_kill_switch_by_human` present | same |
| eleven freezes intact, forgeries refused | 11/11 refused | `artifacts/slice59_registration_discipline.log` |
| ETH/SOL not measured under the cleared name | no such artefact | `test_eth_and_sol_were_not_measured_under_the_cleared_name` |
| paper certification green | exit 0 | `artifacts/slice59_paper_cert.log` |
| full suite green | **3,935 passed, 2 skipped** | `artifacts/slice59_pytest.log` |
| every slice-59 artefact hashed | 8 entries | `artifacts/slice59_sha256_manifest.txt` |

## The two findings a reader should not skip

**1. The promotion gate failed OPEN on its first run, and the test written to
catch that caught it.** `config.is_live_authorized` returns a 2-tuple
`(live_allowed, reason)`. The gate assumed a 3-tuple and read `result[1]` — which
on the real return is the *reason string*, and a non-empty string is truthy. So
the gate reported the live-arming chain satisfied while the shell sat in SANDBOX
(TESTNET+PAPER), and with the checklist flipped it answered **yes**.

That is the worst failure this file could have had, in the one place the design
note promised fail-closed behaviour. The shape is now asserted rather than
guessed, anything unexpected refuses, and five tests pin it — including a control
proving the gate *does* say yes to a well-formed affirmative, so "always refuses"
cannot come to mean "is broken".

**2. There is no forward data, and the artefacts say so rather than implying
otherwise.** The corpus ends 2026-08-09 — the same day the cleared measurement
was taken over. Zero bars have arrived since. The observation record is therefore
*forward-compatible*, not forward: it fixes the schema, caps, schedule and
thresholds a real forward run will use, and declares
`is_forward_observation: false`, `forward_observations_to_date: 0`.

The gate's central item consequently reads **0 / 20** and will keep reading zero
until real time passes. At the pilot's rate — 35 capped trades over 731 days — 20
forward trades is roughly **fourteen months**. That is the intended cost of arming
something that cleared M1 by three rotation replicates.

## What was refused

* **no re-score.** No measurement, control, percentile or fold re-cut;
* **no threshold move.** M-4 warns; it is recorded in the record, in the gate, in
  EDGE §42d and in a test that a later slice would have to *delete* to silence it;
* **no size-up, no schedule change, no constant change**, and no tool flag that
  could produce one;
* **no model, no training path, no live arming**, and no code that could arm;
* **no relabelling of history as experience** — the substitution the gate exists
  to prevent, refused in the one artefact where it would have been easiest.

---

This slice operates the brakes on a thin clear.
It does not deploy an autonomous profit agent.
Closer to bank-grade live autonomy remains NO.
