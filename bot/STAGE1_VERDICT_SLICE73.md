# SLICE 73 — VERDICT

```
SLICE73_VERDICT:                    PASS_WITH_DEFECTS
extension_present:                  YES
after_t1_linear_bars:               11
after_t1_dates:                     ['2026-08-10','2026-08-11','2026-08-12','2026-08-13','2026-08-14','2026-08-15',
                                     '2026-08-16','2026-08-17','2026-08-18','2026-08-19','2026-08-20']
linear_last:                        2026-08-20T00:00:00+00:00
linear_rows:                        1472
new_linear_bars_since_slice72:      1
new_funding_prints_since_slice72:   3      (funding through 2026-08-21T16:00:00Z)
the_window_grew:                    YES
2026-08-21_present_as_linear:       NO   (open bar, correctly absent)

funding_setups_in_window:           1 of 11
08-19_setup:                        YES   SHORT, close-join 0.00010000 == FUND_ABS
08-20_close_join_rate:              0.00009422   (16:00 print — NOT the 08:00 1e-4)
flagged_bars_in_window:             1     2026-08-19   <-- FIRST forward flag
entries_taken:                      0
forward_n_trades:                   0
is_forward_observation:             false
ceiling_max_possible_trades:        6
within_ceiling:                     YES
forward_bars_with_a_barrier_outcome: 4     (what actually bound — §56d)

oos_evidence_re_scored:             NO
shadow_schedule_one_per_run:        YES
constants_fingerprint_match:        YES  662de0115880871352d5d623b1020eaa
fund_abs_unchanged:                 YES  (0.0001)
join_unchanged:                     YES  (at_or_before bar CLOSE)
barrier_function_unchanged:         YES  (skill_test.py, frozen since slice 35)
caps_usd_100:                       YES
monitor_thresholds_unchanged:       YES
forward_monitor_status:             INSUFFICIENT_DATA
promotion_gate_allows_live:         NO   (2 of 8, unchanged)
live_authorized:                    false
models_current_present:             false
frozen_absent_count:                11
universe:                           ("BTCUSDT",)   no ETH/SOL, no Bybit
bars_fabricated:                    0
ProjectStatus.cleared_edge_signal:  funding_carry_fade_btc_v1
Closer to autonomous profit agent?: NO

suite:  baseline 3 failed / 4,626 passed / 2 skipped
        final    0 failed / 4,700 passed / 2 skipped
```

## Why `PASS_WITH_DEFECTS`

Every measurement condition is met and every number verifies from files. The
downgrade is for one thing, and it is mine: **EDGE §56 did not exist in the
repository when the tools ran.** The declared-ceiling ritual exists so the
ceiling is fixed before anyone sees what scoring returns, the evidence for that
is commit order, and this slice does not have it. §56h owns it in full. A clean
PASS would have passed every existing check and blurred exactly the distinction
this programme is built on.

## The measurement

**Ceiling 6, setups 1 of 11, flagged 1, entries 0, closed trades 0.**

```
2026-08-10  0.00005057     2026-08-16  0.00005000
2026-08-11  0.00008282     2026-08-17  0.00009202
2026-08-12  0.00006601     2026-08-18  0.00003650
2026-08-13  0.00007841     2026-08-19  0.00010000   <-- SETUP, now also FLAG
2026-08-14  0.00000629     2026-08-20  0.00009422
2026-08-15  0.00005311     FUND_ABS    0.00010000
```

`2026-08-20` closed, so `2026-08-19` stopped being the corpus's last bar and
became a **flag** — the first the pilot has produced. No entry followed.

**`2026-08-20` is not a second setup.** Its close-join is `0.00009422`, the 16:00
print. A `0.00010000` did print that day at 08:00 and **is not the decision
rate** — the §51c confusion in its most tempting form yet, because this time the
qualifying number sits on the same bar as the decision that rejects it.

## Six states, and the pilot is in the third

The programme has been reporting setups and trades as if one step separated
them. Five do:

| # | state | count |
|---|---|---|
| 1 | SETUP — close-join reaches `FUND_ABS` | **1** of 11 |
| 2 | FLAG — the rule directs a trade there | **1** ← new |
| 3 | ELIGIBLE — a barrier outcome is computable | 0 |
| 4 | CANDIDATE — schedule and lockup admit it | 0 |
| 5 | ENTRY — a fill at the next bar's open | 0 |
| 6 | CLOSED TRADE — an exit inside the corpus | 0 |

The ladder is in the artefact so no rung can stand in for another. Counting a
setup, a flag, or an entry as a closed trade would be the slice-59
substitution, and the gate still reads 0 of 20.

## What actually stopped the entry — and it was not what I predicted

`barrier_r_for_all_bars` marks the corpus tail unscoreable:

```python
eligible[max(0, n - horizon - entry_offset - 1):] = False
```

With `n = 1472`, `horizon = 5`, `entry_offset = 1`, the highest scoreable bar is
**`2026-08-13`, seven bars from the end** — measured on both sides, not
inferred. `2026-08-19` has no barrier outcome, so it never reaches the schedule
and takes no entry. **The gate is arithmetic about available data, not a
judgement about the trade.**

Only **4** forward bars were ever scoreable this slice — 08-10 to 08-13 —
against a declared ceiling of 6 and an unconditional bound of 10. This is the
exact number that was missing from §55c's correction.

**The line reserves one bar more than its own scan needs** (`i ≤ last − 6` would
suffice). It is **not changed**: frozen since slice 35, it scored both sides of
every comparison in EDGE §4–§13 and produced the `n = 41` OOS clear. It was the
most tempting line in the tree this slice and that is precisely the argument for
leaving it alone.

### §55e's prediction, checked against slice 72's frozen artefact

> …the `2026-08-19` setup acquires an entry bar, and — **subject to the
> schedule, the lockup, and the caps** — a forward entry becomes structurally
> possible for the first time.

**First half held**: 08-20 closed, the entry bar exists, the flag appeared.
**Second half was wrong**, and none of the three gates it named is why —
`entries_refused_by_caps` is 0 and the flag never reached the schedule at all.

The honest reading is not that the caution paid off. **A prediction that
enumerates the remaining obstacles claims the list is complete, and mine was
not.** Its two hedges — "the entry is not owed", "a filled entry is not a
result" — are what keep this incomplete rather than false.

## The defect: a design note that never reached the repository

§56a–§56g were composed at STEP 1, before any tool was derived or run. The shell
command that appended them ran in the wrong working directory; the baseline-log
copy, the `EDGE.md` append and the commit all missed the repository, and the
chain reported success. The artefacts were then written at commits `75539e9` and
`c4bf203`, and `git show <commit>:EDGE.md` at either contains **no §56**.

**What survives, stated narrowly.** The ceiling is `max(0, 11 − 5)` — arithmetic
over a frozen `HORIZON` and a bar count verified from files. No free parameter,
the mission fixed 6 independently, and both adjacent errors are asserted
against. **The number is not in doubt; the procedural guarantee is absent.**

**What was not done.** The artefacts were re-run against a tree that now
contains §56, so they stop citing a section that does not exist — and nothing
else. Back-dating the section, or re-running and calling the result
pre-declared, would manufacture the evidence that was lost.

**Guard family member five**:
`TestACitedEdgeSectionMustExistWhenTheArtefactWasWritten`. It runs two checks
and skips neither.

The artefacts now record `edge_state_when_this_ran` — the digest of `EDGE.md`
and the section numbers present — and every cited section must appear in that
record. That form always runs. It is **self-reported and therefore weaker**: a
tool cannot certify its own honesty, and the artefact says so in the field
itself. The stronger form reads `EDGE.md` out of git at the artefact's own
`git_commit` and asserts the same thing; it needs a repository, and **the
deliverable ships without one**, which is how the first version of this guard
was caught — it passed here and failed on a clean unzip of the very zip it was
shipping in.

That failure is the reason both forms exist. A single git-based check would
have had to skip on the deliverable, and **a check that silently skips is the
defect class this programme keeps re-learning** — §52a's presence check that
could not fail. So the weak one always runs and the strong one runs where it
can.

Neither catches a section committed one minute before the tool runs. **That
half is not mechanisable, and claiming otherwise would be this guard repeating
the overclaim it exists to catch.**

## An independent witness for the flag

`test_the_real_setup_counts_are_stable[BTCUSDT]` has held since slice 55 and
failed at this slice's baseline: **417 → 418**, with `long_setups` unchanged at
6, so the new one is a SHORT. It reads the corpus and no artefact, predates the
forward programme, and cannot be argued with.

Amended to 418 **with the cause asserted** — `test_the_btc_movement_is_the_08_19_short`
proves the newest directed signal bar is 08-19, is a short, and is no longer the
last bar. Bumping the number alone would have erased the only evidence that the
movement was understood.

## The expiry list worked

Slice 72 declared thirteen `EXPIRES_WITH_DATA` tests and its verdict handed the
list forward. Two failed at baseline; **both were on the list. Zero unlisted
surprises**, against three a slice earlier. It does not prevent expiry — it
converts a pytest failure into a work item known in advance.

**Slice 74 should amend these twelve** in
`tests/test_slice73_eleven_day_window.py`:

```
test_eleven_closed_bars_lie_after_t1
test_history_is_untouched_and_growth_is_an_append
test_the_open_bar_is_absent
test_only_btc_grew
test_the_counts_came_from_this_slices_files
test_the_flag_appeared_because_08_19_stopped_being_the_last_bar
test_08_20_is_not_a_second_setup
test_that_is_measured_from_the_frozen_function_not_asserted
test_08_19_has_no_barrier_outcome
test_the_reserve_is_one_bar_more_than_the_scan_needs
test_the_btc_pin_is_now_418
test_it_moved_by_exactly_one_and_the_new_one_is_the_08_19_short
```

## Evidence table

| claim | observed | file / command |
|---|---|---|
| window reached 11 | `after_t1_linear` 10 → 11 | `test_eleven_closed_bars_lie_after_t1` |
| growth is real | prefix digests match slice-57 pins | `test_history_is_untouched_and_growth_is_an_append` |
| 08-21 absent | clock-derived | `test_the_open_bar_is_absent` |
| funding not stale | +3 prints through 08-21T16:00Z | `test_the_funding_is_not_stale` |
| all 15 note claims true | scored from files | `test_every_note_claim_is_true` |
| finding says eleven | "ten" asserted absent | `test_the_finding_says_eleven_and_not_ten` |
| **ladder reads 1,1,0,0,0,0** | six states, separately | `test_the_state_ladder_reads_1_1_0_0_0_0` |
| **first forward flag** | 08-19; every prior slice `[]` | `test_the_flag_is_08_19_and_it_is_the_first_ever` |
| the corpus moved, not the rule | 08-19 is no longer last | `test_the_flag_appeared_because_08_19_stopped_being_the_last_bar` |
| 08-20 is not a second setup | close-join 0.00009422 | `test_08_20_is_not_a_second_setup` |
| no entry, no closed trade | candidates `[]`, entries 0 | `test_no_entry_and_no_closed_trade` |
| no rung reported as a higher one | gate evidence says so | `test_a_lower_rung_is_never_reported_as_a_higher_one` |
| **eligibility reaches back 7 bars** | highest scoreable is 08-13 | `test_eligibility_reaches_back_seven_bars` |
| measured from the frozen function | both sides, not asserted | `test_that_is_measured_from_the_frozen_function_not_asserted` |
| 08-19 has no barrier outcome | absent from the index set | `test_08_19_has_no_barrier_outcome` |
| only 4 forward bars scoreable | 08-10 … 08-13 | `test_only_four_forward_bars_were_ever_scoreable` |
| the frozen function untouched | source line unchanged | `test_the_frozen_function_was_not_modified` |
| the reserve is one bar | stated as arithmetic | `test_the_reserve_is_one_bar_more_than_the_scan_needs` |
| **ceiling = 6** | `max(0, 11 − 5)` | `test_the_ceiling_is_six` |
| not 5, not 7 | both mission errors asserted | `test_it_is_not_five_and_not_seven` |
| every prior ceiling lower | 0,0,0,0,0,0,1,2,3,4,5 | `test_every_prior_slice_ceiling_is_recorded_and_lower` |
| every prior numerator zero | from each frozen artefact | `test_every_prior_slice_observed_zero_closed_trades` |
| prediction read from the frozen artefact | slice 72's, not a rewrite | `test_the_prediction_is_read_from_the_frozen_artefact` |
| its first half held | flag appeared | `test_the_predictions_first_half_held` |
| **its gate list was incomplete** | caps refused 0; never reached schedule | `test_the_predictions_list_of_remaining_gates_was_incomplete` |
| the shortfall is in EDGE | §56c | `test_the_shortfall_is_recorded_in_edge` |
| **pin moved 417 → 418** | independent of any artefact | `test_the_btc_pin_is_now_418` |
| the mover is the 08-19 short | newest directed bar | `test_it_moved_by_exactly_one_and_the_new_one_is_the_08_19_short` |
| the witness reads no artefact | asserted on its source | `test_the_witness_reads_no_artefact` |
| **cited sections were present when written** | artefact's own record | `test_every_cited_section_is_in_the_artefacts_own_record` |
| that record matches the shipped EDGE | digest comparison | `test_the_recorded_digest_matches_the_edge_that_ships_beside_it` |
| the ceiling's citation among them | §56a | `test_the_ceilings_declared_in_is_among_them` |
| the same check, from git | when a repository is present | `test_from_git_when_a_repository_is_present` |
| the weaker form is labelled weaker | self-reported | `test_the_self_reported_form_is_labelled_as_weaker` |
| its limits are written down | §56h | `test_the_limitation_is_written_down_not_implied` |
| guards 1–4 carried by import | re-run, not copied | `test_all_four_guards_still_exist_where_expected` |
| scoring not re-tuned | 2 declared changes, 2 declared additions | `test_the_scoring_was_not_re_tuned_since_slice72` |
| expiry list matches the module | AST sweep | `test_the_declared_list_matches_the_module` |
| slice 72's list named both failures | from its own module | `test_slice72s_list_named_both_of_this_slices_failures` |
| nothing was moved | `FUND_ABS`, join, caps, barrier fn | `test_nothing_was_moved_this_slice` |
| gate refuses | 2 / 8 | `test_the_gate_refuses` |
| OOS untouched | `28b7dfe0…`; folds `ff5cc8a2…` | `test_the_oos_clear_was_not_re_scored` |
| live dark | no `models/current` | `test_live_is_dark` |
| registration discipline | HELD | `artifacts/slice73_registration_discipline.log` |
| paper session | green | `artifacts/slice73_paper_cert.log` |
| suite | 4,700 passed / 0 failed / 2 skipped | `artifacts/slice73_pytest.log` |

## Forbidden list — every item, answered

| forbidden | this slice |
|---|---|
| invented bars | `bars_fabricated: 0` |
| 08-21 as closed linear | absent; clock-derived |
| false forward | `is_forward_observation: false`; ladder published |
| Stage-1 re-score | artefact byte-identical |
| `FUND_ABS` / join / caps / schedule / horizon change | all asserted unchanged |
| live / `models/current` / training / Bybit | live dark |
| ETH/SOL expansion | universe `("BTCUSDT",)` |
| frozen reopen | 11 unchanged |
| exit clamping | false |
| autonomy YES | false |
| **claiming ceiling = 5 at N = 11** | asserted `!= 5` |
| **claiming ceiling = 7 at N = 11** | asserted `< 7` |
| counting a setup or entry as a closed trade | ladder; gate reads 0 of 20 |
| `is_forward_observation` true when trades = 0 | false |
| dropping guards | four imported, a fifth added |
| citing missing tests | member four re-run against this verdict |
| finding text still saying TEN | asserted absent |

## Carried forward

1. **Amend the twelve `EXPIRES_WITH_DATA` tests listed above.**
2. **Do STEP 1 in the repository and verify it landed** — `grep '^## 57a\.'
   EDGE.md` and a commit, before deriving a single tool. §56h is what happens
   when that is assumed rather than checked.
3. **An entry needs `2026-08-19` to become scoreable**, which needs the corpus
   to extend seven bars past it — `2026-08-26`. Stated as arithmetic from
   §56d, not as a forecast, and the entry is still not owed.
4. **Four manifest entries remain frozen-wrong**; the funding seam at
   `2026-08-09T16:00Z` is still empty, twelve slices on.

## What would move this forward

A flagged bar that is also **scoreable**, then a candidate, then a fill, then an
exit inside the corpus. Five gates, of which the pilot has now passed two. The
gate asks for 20 completed forward trades and 180 forward days; it has 0 and 11.

**A flag is not an entry, and an entry would not be a result.**
