# SLICE 71 — VERDICT

```
SLICE71_VERDICT:                    PASS
extension_present:                  YES
after_t1_linear_bars:               9
after_t1_dates:                     ['2026-08-10','2026-08-11','2026-08-12','2026-08-13','2026-08-14',
                                     '2026-08-15','2026-08-16','2026-08-17','2026-08-18']
linear_last:                        2026-08-18T00:00:00+00:00
linear_rows:                        1470
new_linear_bars_since_slice70:      1
new_funding_prints_since_slice70:   0
the_window_grew:                    YES
2026-08-19_present:                 NO   (open bar, correctly absent)

forward_n_trades:                   0
forward_observations_to_date:       0
is_forward_observation:             false
ceiling_max_possible_trades:        4
within_ceiling:                     YES
funding_setups_in_window:           0 of 9
08-18_close_join_rate:              0.00003650

oos_evidence_re_scored:             NO
shadow_schedule_one_per_run:        YES
constants_fingerprint_match:        YES  662de0115880871352d5d623b1020eaa
fund_abs_unchanged:                 YES  (0.0001)
join_unchanged:                     YES  (at_or_before bar CLOSE)
caps_usd_100:                       YES
monitor_thresholds_unchanged:       YES
forward_monitor_status:             INSUFFICIENT_DATA
promotion_gate_allows_live:         NO   (2 of 8, unchanged)
live_authorized:                    false
models_current_present:             false
frozen_absent_count:                11
bars_fabricated:                    0
ProjectStatus.cleared_edge_signal:  funding_carry_fade_btc_v1
Closer to autonomous profit agent?: NO

suite:  baseline 1 failed / 4,499 passed / 2 skipped
        final    0 failed / 4,563 passed / 2 skipped
```

## The measurement

**Ceiling 4, setups 0 of 9, fills 0.**

```
2026-08-10  0.00005057     2026-08-15  0.00005311
2026-08-11  0.00008282     2026-08-16  0.00005000
2026-08-12  0.00006601     2026-08-17  0.00009202
2026-08-13  0.00007841     2026-08-18  0.00003650
2026-08-14  0.00000629     FUND_ABS    0.00010000
```

Every rate standing at a decision is below the threshold. It says the funding
regime from 2026-08-10 to 2026-08-18 never reached `FUND_ABS` at a decision. It
says nothing about whether the rule makes money: no position was opened, so only
**selectivity** has been exercised, never skill.

**The denominator has gone 0 → 1 → 2 → 3 → 4. The numerator has never moved.**

## Three near misses, and the newest one is the reassuring one

```
08-12 00:00   0.00010000   == FUND_ABS, superseded before close   -> JOIN change
08-17 16:00   0.00009202   stood at close, short by 8.0e-6        -> FUND_ABS change
08-18 16:00   0.00003650   stood at close, short by 6.4e-5        -> nothing
```

The mission predicted `2026-08-18` at roughly `3.650e-5` and the file agrees
exactly. **It is farther from the line than 08-17, not closer** — and that is
the useful part. A threshold whose misses are all narrow looks like a boundary
being crept toward; one that misses widely the very next day is behaving like a
threshold. Nothing moves: `FUND_ABS` 0.0001, `HORIZON` 5, join at bar close.

**A property now asserted rather than assumed.** `close_time_ms` falls back to
`start_ms + 86_400_000 − 1` and these bars carry no `end_us`, so the decision
instant for 2026-08-18 is `23:59:59.999Z` — strictly before the next day's 00:00
print. **A funding print stamped exactly at midnight could not retroactively
revise the newest decision.** The join is final the moment the bar closes.

## Two things arrived looking like changes and were not

This slice's substantive finding is a matched pair, in opposite directions.

**The funding corpus did not change, and its file digest did.**

```
funding .csv.gz   compressed    b533d2ed…  ->  b195c5e6…    DIFFERENT
                  uncompressed  fa8b207f…  ->  fa8b207f…    IDENTICAL
                  rows 4410 -> 4410,  last print 2026-08-18T16:00:00Z, unchanged
```

It was recompressed; not one print changed. Every earlier instance of §46c's
digest trap sat beside a corpus that had genuinely grown, so a reader conflating
the two still reached the right verdict by luck. **Here the naive check is
simply wrong** — comparing compressed digests reports a change that did not
happen. `new_funding_prints_since_slice70` is **0**, and the artefact now carries
the predecessor's uncompressed funding digest so the claim is checkable.

**A manifest changed format, not content.**

```
                       slice 70          slice 71
BTCUSDT 1D end         "2026-08-17"      "2026-08-18T00:00:00+00:00"
BTCUSDT FUNDING end    "2026-08-18"      "2026-08-18T16:00:00+00:00"
```

`_manifest_audit` compared a date against what is now a timestamp. On the
delivered tree that is False for both BTC entries, so a strict audit would have
reported **six** disagreeing entries where there are **four**, and flipped
`measured_product_entries_are_accurate` to false while BTC's data matched its
record exactly.

Same lesson mirrored: identical content wearing different bytes, identical
content wearing a different string. **Compare the thing you mean, not the thing
that is easy to compare.** The audit now reports the strict comparison, the
content comparison, and a computed `declared_end_format_changed_this_slice` — the
format change is **surfaced, not smoothed away**, because a provenance record
whose encoding moves without notice is worth seeing. It just does not get to be
called a disagreement.

## The guard family was carried — and it fired twice on me

The mission required all four guards plus scoring equality. They are **imported
from the slices that wrote them rather than copied**, so there is one
implementation of each rule and this module cannot drift from it. That is the
direct fix for how slices 67–69 lost the scoring guard: a fresh test file that
simply did not carry it.

Both members fired during authoring, on this slice's own work:

| guard | caught |
|---|---|
| `TestNoTestPinsALiveAbsolute` (66) | a chained `rows_on_disk == … == 1470` in **this module** |
| `TestNoToolCarriesAnotherSlicesConstant` (69) | `"unchanged from slice 69: "` left in the gate tool |

Neither reached the artefacts. **A guard that never fails is not known to be a
guard**; these two failed on the author, which is where they are supposed to.

The scoring comparison runs against slice 70 with three declared changes
(`slice`, `ceiling`, `why_neither_moved`), no additions and no dropped keys.
`build`'s body is AST-identical.

## The one baseline failure, and why it is not a loophole

Slice 70's `test_the_counts_came_from_files` asserted

```python
cp.check(cp.LINEAR_BTC).rows_on_disk == load(FRESHNESS)["linear_rows"]
```

against **slice 70's own** artefact — which member three explicitly permits,
because at authoring time it had just been written from that disk in that run.
One slice later the same line is a live count equated to a **dated** artefact:
the shape member three forbids.

**No AST sweep can catch this**, because the code was correct when written and
became wrong only by the passage of a slice. The remedy is the standing rule
that the current slice owns the live apparatus. Recorded in §54f because the
other reading — "member three has a loophole" — is wrong and would invite
weakening a guard that is working. The narrower lesson: **an assertion's
correctness can have an expiry date even when nothing about it was careless.**

Third consecutive slice in which the substitution pipeline failed to update
`SLICE = 70`. It is now derived into every payload, so the number lives in one
place per tool and a test is aimed at that place.

## Evidence table

| claim | observed | file / command |
|---|---|---|
| window reached 9 | `after_t1_linear` 8 → 9 | `test_nine_closed_bars_lie_after_t1` |
| growth is real | prefix digests match slice-57 pins; append-only | `test_history_is_untouched_and_growth_is_an_append` |
| only the linear corpus grew | funding delta 0; ETH/SOL 0 | `test_only_the_linear_corpus_grew` |
| 08-19 absent | clock-derived, no literal beside it | `test_the_open_bar_is_absent` |
| all 11 note claims true | scored from files | `test_every_note_claim_is_true` |
| note path names slice 71 | derived; `SLICE == 71` asserted | `test_the_note_path_names_this_slice_and_slice_is_derived` |
| slice number derived everywhere | no payload literal in any tool | `test_the_slice_number_is_derived_in_every_tool` |
| **ceiling = 4** | `max(0, 9 − 5)`, declared §54a before the run | `test_the_ceiling_is_four` |
| not 3, not 5 | both mission errors asserted against | `test_it_is_not_three_and_not_five` |
| every prior ceiling lower | 0,0,0,0,0,0,1,2,3 from frozen artefacts | `test_every_prior_slice_ceiling_is_recorded_and_lower` |
| every prior numerator zero | read from each frozen artefact | `test_every_prior_slice_observed_zero` |
| 0 setups of 9 | every decision rate below `FUND_ABS` | `test_zero_setups_on_all_nine_bars` |
| per-bar table, all 9 dates | date, rate, setup flag, shortfall | `test_the_per_bar_table_covers_every_date` |
| 08-18 = 0.00003650 | reproduced from the join | `test_the_join_is_unchanged_and_reproduces_all_three` |
| newest miss is the widest | 6.4e-5 vs 8.0e-6 | `test_the_newest_miss_is_the_widest_of_the_three` |
| newest decision is final | close instant is 23:59:59.999Z | `test_the_newest_decision_cannot_be_revised_later` |
| all three misses cited | 08-12, 08-17, 08-18 in `why_neither_moved` | `test_the_reason_none_moved_cites_all_three` |
| **funding unchanged** | identical uncompressed stream and rows | `test_the_uncompressed_stream_is_identical_to_slice70s` |
| its digest moved anyway | compressed digest differs | `test_the_compressed_digest_moved_and_says_nothing` |
| the pin is unaffected | prefix digest still matches folds | `test_the_repository_pins_the_uncompressed_stream` |
| **manifest changed format** | `end` is now a timestamp | `test_the_declared_end_is_now_a_timestamp` |
| strict would have said six | 4 real, 6 strict | `test_a_strict_comparison_would_have_reported_six` |
| BTC entries still accurate | rows and content-end agree | `test_the_measured_products_entries_are_still_accurate` |
| the change is surfaced | `declared_end_format_changed_this_slice` | `test_the_format_change_is_surfaced_not_smoothed` |
| ETH/SOL untouched | residue unchanged, no broadcast | `test_eth_and_sol_are_untouched_in_both_senses` |
| guards 1–4 carried | imported and re-run, not copied | `test_all_four_guards_still_exist_where_this_module_expects_them` |
| scoring not re-tuned | AST-equal; 3 declared changes, 0 additions | `test_the_scoring_was_not_re_tuned_since_slice70` |
| the guard is where 72 will look | asserted in this module | `test_the_guard_is_where_the_next_slice_will_look_for_it` |
| no restore needed | artefact absent; fourth clean pack | `test_no_restoration_was_needed_again` |
| pack base true | EDGE §45a–§53a in the delivered tree | `test_the_pack_base_claim_is_true_this_time` |
| no edge measurement ran | artefacts carry no percentile/replicates | `test_no_edge_measurement_ran_this_slice` |
| gate refuses | 2 / 8, `allows_live` false | `test_the_gate_refuses` |
| gate refuses 4 possible trades | evidence says so in words | `test_the_gate_refuses_to_credit_four_possible_trades` |
| OOS untouched | `28b7dfe0…`; folds `ff5cc8a2…` | `test_the_oos_clear_was_not_re_scored` |
| live dark | no `models/current`, `policy_mode` off | `test_live_is_dark` |
| registration discipline | HELD | `artifacts/slice71_registration_discipline.log` |
| paper session | green | `artifacts/slice71_paper_cert.log` |
| suite | 4,563 passed / 0 failed / 2 skipped | `artifacts/slice71_pytest.log` |

## Forbidden list — every item, answered

| forbidden | this slice |
|---|---|
| synthetic / invented / forward-filled bars | `bars_fabricated: 0` |
| including 2026-08-19 as a closed linear bar | absent; clock-derived assertion |
| relabelling bars ≤ `t1` as forward | strict at `t1` |
| trusting the note without verifying | 11 claims scored from files |
| re-scoring OOS / Stage-1 | artefact byte-identical |
| changing constants, schedule, caps, monitors | fingerprint `662de011…` |
| changing `FUND_ABS` or the join | both `false`, all three misses cited |
| raising `max_notional_usd` above 100 | 100.00 |
| arming live, `models/current`, training, Colab, GPUs | live dark; no weights in tree |
| reopening frozen families / adding alts | 11 unchanged; universe `("BTCUSDT",)` |
| `closer_to_autonomous_profit_agent = true` | false |
| shadow mean R as Stage-1 evidence | `forward_mean_net_r: null` |
| completing human checklist items in code | 0 |
| clamping unfinished exits | false; last bar flagged unable to fill |
| claiming N=9 when `after_t1 ≠ 9` | it is 9, from disk |
| **claiming ceiling = 3 at N = 9** | asserted `!= 3` |
| **claiming ceiling ≥ 5 at N = 9** | asserted `< 5` |
| finding text still saying "eight" | asserted absent in three findings |
| stale `human_note_path` (SLICE70) | `…SLICE71.md`, derived, `SLICE == 71` asserted |
| dropping the slice-70 guard family | all four imported and re-run |
| "almost positive" / multi-symbol rescue | absent |
| "fixing" the ETH/SOL manifest residue | not edited; residue reported as residue |

## Carried forward

1. **Four manifest entries remain frozen-wrong.** ETH/SOL linear should read
   1461; ETH funding 4383; SOL funding 4458. Unedited: rewriting a provenance
   record to agree with disk certifies nothing.
2. **The funding seam at `2026-08-09T16:00Z` is still empty**, ten slices on.
3. **The funding corpus has not gained a print in two slices.** It ends at
   `2026-08-18T16:00Z`. This did not affect the measurement — the newest
   decision instant precedes midnight — but if linear keeps advancing while
   funding does not, later decisions will join to increasingly stale rates.
   That would be a data-freshness defect, and it is worth watching before it is
   worth reporting.
4. **Slice 72 must amend `test_the_counts_came_from_this_slices_files`.** It is
   correct today and becomes a live-vs-dated equality the moment a bar arrives.

## What would move this forward

A funding rate **at or above `1e-4` standing at a bar's close**, then a barrier
resolving inside the window. The window is long enough and getting longer; the
regime is not obliging. The gate asks for 20 completed forward trades and 180
forward days; the pilot has 0 and 9.

**Nine quiet days is unremarkable against a cleared run that scheduled 41 trades
in two years — one per eighteen days. The ceiling grew because a day closed.**
