# SLICE 72 — VERDICT

```
SLICE72_VERDICT:                    PASS
extension_present:                  YES
after_t1_linear_bars:               10
after_t1_dates:                     ['2026-08-10','2026-08-11','2026-08-12','2026-08-13','2026-08-14',
                                     '2026-08-15','2026-08-16','2026-08-17','2026-08-18','2026-08-19']
linear_last:                        2026-08-19T00:00:00+00:00
linear_rows:                        1471
new_linear_bars_since_slice71:      1
new_funding_prints_since_slice71:   6      (funding through 2026-08-20T16:00:00Z)
the_window_grew:                    YES
2026-08-20_present_as_linear:       NO   (open bar, correctly absent)

forward_n_trades:                   0
forward_observations_to_date:       0
is_forward_observation:             false
ceiling_max_possible_trades:        5
within_ceiling:                     YES
unconditional_upper_bound:          9      (see §55c)
funding_setups_in_window:           1 of 10        <-- FIRST forward setup
08-19_close_join_rate:              0.00010000     == FUND_ABS exactly

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
universe:                           ("BTCUSDT",)   no ETH/SOL, no Bybit
bars_fabricated:                    0
ProjectStatus.cleared_edge_signal:  funding_carry_fade_btc_v1
Closer to autonomous profit agent?: NO

suite:  baseline 3 failed / 4,560 passed / 2 skipped
        final    0 failed / 4,629 passed / 2 skipped
```

## The measurement

**Ceiling 5, setups 1 of 10, fills 0.**

```
2026-08-10  0.00005057     2026-08-15  0.00005311
2026-08-11  0.00008282     2026-08-16  0.00005000
2026-08-12  0.00006601     2026-08-17  0.00009202
2026-08-13  0.00007841     2026-08-18  0.00003650
2026-08-14  0.00000629     2026-08-19  0.00010000   <-- SETUP
                           FUND_ABS    0.00010000
```

**The first forward setup the pilot has ever produced.** `2026-08-19`'s 16:00
print is `0.00010000`, it is the last print before the bar's close, and
`funding_setups` compares `f >= fund_abs` inclusively. It is a SHORT setup.

**It is not the same event as 2026-08-12.** That print also equalled `FUND_ABS`
and was superseded by two later prints before its bar closed, so it never became
a decision rate. §51c has kept *"a qualifying rate existed that day"* and *"the
qualifying rate stood at the decision"* apart since slice 68; **this is the first
time the second one is true**, and four slices of maintaining that distinction is
what makes today's claim precise rather than a rediscovery.

**It is not a trade and could not become one on this tree.** The rule enters at
`next_open`. `2026-08-19` is the last bar in the corpus, so there is no bar to
fill on, and `directed_signal_bars` excludes the final bar for exactly this
reason:

```
funding_setup_present   2026-08-19    True
flagged_bars_in_window                []
candidates_after_schedule             []
entries_taken                         0
forward_n_trades                      0
is_forward_observation                false
```

**No parameter moved to produce it.** `FUND_ABS` is 0.0001 and the market
printed 0.00010000. That matters more than the setup does: three near misses
across slices 70 and 71 were each left unrescued, so a threshold reached now is
a threshold reached *by the market*. **A threshold reached because it was
lowered would be worth nothing**, and the only reason that distinction is
available is that nothing was moved while the misses accumulated.

**The numerator is still 0.** The gate reads 0 of 20 trades and 0 of 180 days.

## A prediction, recorded in §55e before this tool ran

If `2026-08-20` arrives as a closed bar, the `2026-08-19` setup acquires an
entry bar and — subject to the schedule, lockup and caps — a **forward entry**
becomes structurally possible for the first time. A *completed* trade needs the
exit too: `2026-08-21` at the earliest, `2026-08-25` at the latest.

Written now so it cannot be reverse-fitted, and with both halves stated: **the
entry is not owed, and a filled entry is not a result.** If 08-20's close-join
falls back below `FUND_ABS` the run ends without an entry, and that is equally
admissible. `is_forward_observation` stays false until a trade CLOSES.

## The ceiling does not bound what ten slices of artefacts said it bounds

Declaring the ceiling for the eleventh time, with a setup finally on the board,
is the moment to check what the number actually bounds.

`max(0, N − HORIZON)` counts decision bars **on the assumption that every trade
holds for the full horizon**. The code does not assume that —
`held = side_used.get(index, HORIZON)` — and measured over this corpus the
short-side barrier resolves early **52.8%** of the time:

```
hold length    0     1     2     3     4     5
count        147   169   170   155   124   685
```

So a decision later than `last − HORIZON` can also close inside the window, and
the unconditional structural bound is `N − 1 = 9`, not 5.

**The declared number stands.** It was pre-declared in §55a, the mission
mandates it, and `observed (0) ≤ 5` holds. Re-deriving a bound after seeing that
a setup fired is precisely the move this programme refuses everywhere else.

**The `if_exceeded` sentence was wrong and is corrected.** Every artefact since
slice 62 said *"a forward trade count above this ceiling is a DATA DEFECT or a
scheduling bug"*. It is not: with early resolution it can be ordinary and
correct. Both bounds and the assumption each rests on are now in the artefact.
The prior artefacts keep what they said —
`test_the_old_claim_is_visible_in_the_frozen_artefacts` asserts the old wording
is still there in slices 68, 70 and 71.

It never mattered, because the numerator never moved. It is fixed in the slice a
setup first appeared and **before** a trade can exist, which is the only time a
bound can be corrected without the correction looking chosen to fit a result.

## The funding is not stale — and the check that says so now works both ways

Slice 71 flagged this as a watch item and the mission makes it a FAIL condition.

```
funding rows           4410 -> 4416          +6 prints
funding last print     2026-08-18T16:00Z -> 2026-08-20T16:00Z
funding UNCOMPRESSED   fa8b207f… -> 44c8deda…    GENUINELY DIFFERENT
```

Slice 71 established that a moved *compressed* digest proves nothing, and its
uncompressed check answered "identical — recompressed, not changed". The same
check answers "changed" here. **A test that has only ever returned one answer is
indistinguishable from a constant**; this one has now returned both, which is
what turns it from an assertion into an instrument.

The human note quotes an **uncompressed** funding digest this slice — the first
time a note has quoted the measure the repository actually pins. It is scored
like any other claim, and it is true.

## A field that was inverted relative to its name

The same "second observation" logic caught a defect I shipped in slice 71.

`funding_covers_the_newest_decision` was implemented as
`last_print <= newest_decision_instant` — **true exactly when the funding corpus
stops short**, which is the stale condition the name rules out. In slice 71 it
returned `True` while describing marginal coverage; this slice, with strictly
better coverage, it went `False`.

Nothing consumed it — it was a reporting field — so no measurement was affected,
and that is the whole blast radius rather than a minimisation of it. It is split
into two fields that say what they mean (`covers`, `extends_past`), and
`test_the_old_field_would_have_answered_the_wrong_question` demonstrates the
inversion rather than describing it.

**How it was caught is the lesson.** Not by review: by a boolean seeing its
second input. Slice 71's saw one.

## Prose that could only describe one outcome

Two artefacts carried the literal words **"ZERO SETUPS FIRED"**. This is the
slice on which that became false. Both are now derived from the setup count, and
`test_the_finding_prose_is_derived_not_a_constant` asserts the old string cannot
come back.

This is §53d's rule paying off in the direction it was written for: a sentence
that can only ever describe one outcome is not a report, and the slice where it
goes wrong is exactly the slice where it matters most.

## Assertions that expire: a list, not a surprise

Slice 71's three baseline failures were all live-disk assertions that were
correct when written and expired when a bar arrived — in three spellings, one of
which (`len(rows) == 4410`) is a live absolute pinned to a literal in a form
slice 66's guard does not recognise.

No sixth guard can help: expiry is not a defect at authoring time (§54f). What
is controllable is whether the next slice inherits a **list** or a failure. So
this module declares `EXPIRES_WITH_DATA` — the names of its own tests that read
live corpus state — checked by an AST sweep against the module itself. A name
missing from the list fails; a name on the list that reads nothing live fails
too.

**Slice 73 should amend these thirteen**, all in
`tests/test_slice72_ten_day_window.py`:

```
test_ten_closed_bars_lie_after_t1
test_history_is_untouched_and_growth_is_an_append
test_the_funding_covers_the_newest_decision
test_the_old_field_would_have_answered_the_wrong_question
test_only_btc_grew
test_the_open_bar_is_absent
test_a_funding_print_dated_after_the_last_bar_is_not_a_bar
test_the_counts_came_from_this_slices_files
test_the_rate_is_reproduced_from_the_corpus_not_the_artefact
test_the_qualifying_print_stood_at_the_close
test_it_is_not_the_same_event_as_08_12
test_barriers_resolve_early_more_than_half_the_time
test_the_three_misses_reproduce_from_the_corpus
```

Most will still pass; the ones that will not are the ones asserting what the
*last* bar is. The point is that nobody has to find out by running pytest.

## Evidence table

| claim | observed | file / command |
|---|---|---|
| window reached 10 | `after_t1_linear` 9 → 10 | `test_ten_closed_bars_lie_after_t1` |
| growth is real | prefix digests match slice-57 pins; append-only | `test_history_is_untouched_and_growth_is_an_append` |
| **funding not stale** | +6 prints, uncompressed digest moved | `test_the_funding_is_not_stale` |
| the check answers both ways | "identical" in 71, "changed" in 72 | `test_the_uncompressed_check_now_answers_both_ways` |
| 08-20 is not a linear bar | funding prints exist; no bar does | `test_a_funding_print_dated_after_the_last_bar_is_not_a_bar` |
| open bar absent | clock-derived | `test_the_open_bar_is_absent` |
| all 14 note claims true | scored from files | `test_every_note_claim_is_true` |
| note quoted the pinned measure | uncompressed funding digest | `test_the_note_quoted_the_measure_the_repository_pins` |
| note path names slice 72 | derived; `SLICE == 72` asserted | `test_the_note_path_names_this_slice_and_slice_is_derived` |
| **first forward setup** | 1 of 10, `2026-08-19`, SHORT | `test_exactly_one_forward_decision_reached_fund_abs` |
| reproduced from the corpus | not read out of the artefact | `test_the_rate_is_reproduced_from_the_corpus_not_the_artefact` |
| it stood at the close | 16:00 is the last print of the day | `test_the_qualifying_print_stood_at_the_close` |
| not the same event as 08-12 | that one joined at 0.00006601 | `test_it_is_not_the_same_event_as_08_12` |
| **setup ≠ trade** | no flag, no candidate, no entry | `test_the_setup_produced_no_flag_no_candidate_no_entry_no_trade` |
| excluded by the rule, not the line | final bar has no next bar | `test_the_reason_is_the_rule_not_a_shortfall` |
| nothing was moved to produce it | `FUND_ABS` 0.0001, fingerprint matches | `test_no_parameter_moved_to_produce_it` |
| not counted as gate progress | 0 of 20; `closer` false | `test_the_setup_is_not_counted_as_a_trade_anywhere` |
| **ceiling = 5** | `max(0, 10 − 5)`, declared §55a before the run | `test_the_ceiling_is_five` |
| not 4, not 6 | both mission errors asserted against | `test_it_is_not_four_and_not_six` |
| formula not re-derived | still `max(0, N − HORIZON)` | `test_the_declared_formula_was_not_re_derived` |
| **barriers resolve early 52.8%** | measured over the corpus | `test_barriers_resolve_early_more_than_half_the_time` |
| unconditional bound reported | `N − 1 = 9` | `test_the_unconditional_bound_is_reported` |
| `if_exceeded` corrected | names §55c and the right bound | `test_the_if_exceeded_claim_was_corrected` |
| history not rewritten | old wording still in 68/70/71 | `test_the_old_claim_is_visible_in_the_frozen_artefacts` |
| every prior ceiling lower | 0,0,0,0,0,0,1,2,3,4 | `test_every_prior_slice_ceiling_is_recorded_and_lower` |
| every prior numerator zero | read from each frozen artefact | `test_every_prior_slice_observed_zero_trades` |
| **coverage field was inverted** | demonstrated, not described | `test_the_old_field_would_have_answered_the_wrong_question` |
| prose is derived | "ZERO SETUPS FIRED" cannot return | `test_the_finding_prose_is_derived_not_a_constant` |
| three misses unrescued | reproduced from the corpus | `test_the_three_misses_reproduce_from_the_corpus` |
| restraint is what makes it evidence | recorded in `why_not` | `test_the_restraint_is_what_makes_the_setup_evidence` |
| guards 1–4 carried by import | re-run, not copied | `test_all_four_guards_still_exist_where_expected` |
| scoring not re-tuned | AST-equal; 3 declared changes, 0 additions | `test_the_scoring_was_not_re_tuned_since_slice71` |
| expiring assertions declared | AST sweep vs the declared list | `test_the_declared_list_matches_the_module` |
| the list is handed forward | this verdict carries it | `test_the_verdict_hands_the_list_forward` |
| no restore needed | artefact absent; fifth clean pack | `test_no_restoration_was_needed_again` |
| pack base true | EDGE §45a–§54a in the delivered tree | `test_the_pack_base_claim_is_true_this_time` |
| no edge measurement ran | no percentile/replicates in artefacts | `test_no_edge_measurement_ran_this_slice` |
| gate refuses | 2 / 8, `allows_live` false | `test_the_gate_refuses` |
| gate refuses 5 possible trades | and says a setup is not a trade | `test_the_gate_refuses_to_credit_five_possible_trades` |
| OOS untouched | `28b7dfe0…`; folds `ff5cc8a2…` | `test_the_oos_clear_was_not_re_scored` |
| BTC only, 11 frozen | universe `("BTCUSDT",)` | `test_eleven_families_stay_frozen_and_the_universe_is_btc_only` |
| live dark | no `models/current`, `policy_mode` off | `test_live_is_dark` |
| registration discipline | HELD | `artifacts/slice72_registration_discipline.log` |
| paper session | green | `artifacts/slice72_paper_cert.log` |
| suite | 4,629 passed / 0 failed / 2 skipped | `artifacts/slice72_pytest.log` |

## Forbidden list — every item, answered

| forbidden | this slice |
|---|---|
| invented bars | `bars_fabricated: 0` |
| 08-20 as a closed linear bar | absent; funding prints for that date are not bars |
| false forward | `is_forward_observation: false`; a setup is not a trade |
| Stage-1 re-score | artefact byte-identical |
| `FUND_ABS` / join / caps / schedule / horizon change | all asserted unchanged |
| live / `models/current` / training / Bybit | live dark; no weights in tree |
| ETH/SOL expansion | universe `("BTCUSDT",)`; family stays ABSENT |
| frozen reopen | 11 unchanged |
| exit clamping | false |
| autonomy YES | false |
| **claiming ceiling = 4 at N = 10** | asserted `!= 4` |
| **claiming ceiling ≥ 6 at N = 10** | asserted `< 6` |
| linear growth with funding still ending 08-18T16:00 | +6 prints through 08-20T16:00Z |
| dropping guards | all four imported and re-run |
| citing missing tests | member four re-run against this verdict |
| finding text still saying "nine" | asserted absent; says TEN |
| "fixing" the ETH/SOL residue | not edited |

## Carried forward

1. **Amend the thirteen `EXPIRES_WITH_DATA` tests listed above.**
2. **If `2026-08-20` closes, check §55e's prediction against the frozen
   artefacts** — as slice 68 did with §49b — not against a fresh computation.
3. **Four manifest entries remain frozen-wrong.** ETH/SOL linear should read
   1461; ETH funding 4383; SOL funding 4458. Unedited.
4. **The funding seam at `2026-08-09T16:00Z` is still empty**, eleven slices on.
5. **`2026-08-20T08:00Z` also printed `0.00010000`.** It belongs to a bar that
   has not closed and is therefore not a decision. Noted so that next slice's
   close-join is checked from the file rather than assumed from this line.

## What would move this forward

An **entry**, then a barrier resolving inside the window, then a **close**. The
window is long enough, the regime has now produced one qualifying decision, and
none of that is a completed trade. The gate asks for 20 and 180; the pilot has
0 and 10.

**One setup in ten days is an entirely ordinary rate for a rule that scheduled
41 trades in two years. The numerator has still never moved.**
