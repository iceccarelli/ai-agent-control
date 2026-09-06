# SLICE 60 — VERDICT

```
SLICE60_VERDICT:                    PASS
cleared_edge_signal:                funding_carry_fade_btc_v1
oos_re_scored:                      NO
new_bars_available:                 NO
is_forward_observation:             false
forward_observations_to_date:       0
shadow_schedule_one_per_run:        YES   (policy restated; not re-run — no new data)
caps_usd_100:                       YES   (100.00, untouched)
monitor_thresholds_unchanged:       YES   (identical; not re-run, deliberately)
monitor_status:                     N/A this slice — slice 59's WARN stands
promotion_gate_allows_live:         NO    (2 of 8 complete, unchanged)
human_templates_present:            YES   (three, unsigned)
live_authorized:                    false
models_current_present:             false
frozen_absent_count:                11
Closer to autonomous profit agent?: NO
```

## Evidence table

| claim | observed | file / command |
|---|---|---|
| baseline all-true before any change | 19/19 rows; 3,935 passed, 2 skipped | `artifacts/slice60_baseline_pytest.log` |
| design note precedes all code | EDGE §43 commit `--stat` lists `EDGE.md` + baseline log only | `git show --stat` |
| "new" defined before looking | strictly after `t1`, **closed only**, `synthetic: false` | `EDGE.md` §43b |
| the ceiling was computed before the fetch | max possible new closed bars = **0**, derived from the clock | `slice60_data_freshness.json → closed_bar_ceiling` |
| **the fetch was attempted anyway** | `fetch_attempted: true` | `test_the_fetch_was_actually_attempted` |
| the failure is recorded verbatim | `ProxyError`, message + traceback tail, `rows_returned: 0` | `slice60_data_freshness.json → fetch` |
| no second route to the venue was tried | only the in-repo sanctioned fetcher is called | `tools/data_freshness_check.py` |
| **0 new closed bars** | `new_linear_bars_count: 0`, `new_bars_available: false` | `test_new_bars_available_is_false` |
| the count never exceeds the ceiling | 0 ≤ 0 | `test_the_count_never_exceeds_the_ceiling` |
| nothing fabricated, no corpus appended | `bars_fabricated: 0`, `corpus_appended_to: false` | `test_nothing_was_fabricated_or_appended` |
| **the 1 new funding print produces nothing** | explained, not counted as progress | `test_the_single_new_funding_print_is_explained_not_counted` |
| the HOLD says HOLD | `result: HOLD`, `is_forward_observation: false`, `forward: 0` | `test_it_says_HOLD` |
| **the monitors were NOT re-run** | `monitors_re_run: false`, readings `null` | `test_the_monitors_were_not_re_run` |
| it points at slice 59 rather than copying | source named; values match slice 59 exactly | `test_the_pointed_at_readings_match_slice59_exactly` |
| M-4 still WARN, still not silenced | earlier +0.4669 on 17, recent −0.0798 on 18 | `slice59_forward_shadow.json` |
| thresholds untouched | live map == slice 58 == slice 59 | `test_the_caps_and_thresholds_are_untouched` |
| caps untouched | 1 / 1 / 100.00 | same |
| folds not re-cut | hash matches lock log; `t1` unchanged | `test_the_folds_were_not_re_cut` |
| clear not re-scored | `cleared_edge_re_scored_this_slice: false`; no slice-60 edge/control/summary artefact | `test_the_clear_was_not_re_scored` |
| the HOLD cannot register | refused even renamed `x_summary.json` | `test_dropping_it_anywhere_registers_nothing` |
| the clear still comes from slice 57 | n=41, M1 95.13, M2 96.0 | `test_the_clear_still_comes_from_the_slice57_artefact` |
| **three templates exist, unsigned** | signature lines still underscore runs | `test_the_memo_signature_blocks_are_empty` |
| the memo states the thinness | "three rotation replicates", "NOT held-out data" | `test_the_memo_states_the_thinness_rather_than_selling_it` |
| the memo forbids moving M-4 | "not one of the options" | `test_the_memo_forbids_moving_the_m4_threshold` |
| the stop checklist names the real gap | `NotImplementedError`, "never been exercised end to end" | `test_the_stop_checklist_names_the_simulator_gap` |
| **templates are not a checklist item** | absent from `CHECKLIST_ITEMS`; gate never reads `docs/promotion` | `test_templates_are_not_a_checklist_item` |
| the completed count did not change | 2/8, equal to slice 59 | `test_the_completed_count_did_not_change` |
| no signature forged | `signatures_present: false`, `signatures_forged: false` | `test_no_signature_was_forged` |
| **gate still refuses, both files** | `allows_live: false`, 6 incomplete | `test_the_gate_refuses_from_both_files` |
| live dark, model absent | live false, policy off, paper, `models/current` absent | `artifacts/slice60_project_status.json` |
| eleven freezes, forgeries refused | 11/11 | `artifacts/slice60_registration_discipline.log` |
| paper certification green | exit 0 | `artifacts/slice60_paper_cert.log` |
| full suite green | **3,993 passed, 2 skipped** | `artifacts/slice60_pytest.log` |
| every artefact and template hashed | 12 entries | `artifacts/slice60_sha256_manifest.txt` |

## The three substitutions this slice refused

**1. Calling the HOLD a forward observation.** There are zero closed bars after
`t1 = 2026-08-09T00:00:00Z`, and the arithmetic proving the ceiling is zero was
recorded in §43b *before* the fetch ran. The fetch was attempted anyway — "I
reasoned it must be zero" is weaker than "I looked, and here is the error" — and
it failed with a `ProxyError` recorded verbatim.

**2. Re-running the monitors on unchanged data.** This is the subtle one. Nothing
would have looked wrong about a slice-60 artefact carrying four fresh-looking
monitor readings; they would even have been *correct*. But they would have been
the same numbers over the same trades, presented as a new observation. The record
carries `monitors_re_run: false` and points at slice 59's readings instead.

**3. Letting a template complete a checklist item.** Three documents now exist
with blank signature blocks. `templates_present: true` is recorded **outside** the
checklist, `CHECKLIST_ITEMS` contains no template entry, and the gate never reads
`docs/promotion`. The completed count is unchanged at 2 of 8. A template is a
form; a memo is a decision.

## One thing that looked like progress and was not

There **is** one new funding print after `t1` — 2026-08-09T08:00:00Z. It produces
nothing: the decision clock is the daily bar, a funding rate is joined to the last
bar whose close it precedes, and no bar after `t1` has closed. Reporting "1 new
funding print" without that sentence would read as partial progress where there is
none, so the sentence is in the artefact and a test asserts it.

## What the templates say that matters

The linear stop-verification checklist names the concrete reason that item is not
a formality: **the linear simulator raises `NotImplementedError`**, so the
protective-stop guarantee this repository asserts everywhere has never been
exercised end to end in simulation on the instrument this signal actually trades.
That is the item most likely to be waved through and the one least safe to wave
through, and it is now written down where a signer must read it.

---

Zero or more true forward bars must be stated honestly.
This slice does not deploy an autonomous profit agent.
Promotion remains REFUSE.
