# SLICE 75 — VERDICT

```
SLICE75_VERDICT:                    PASS
extension_present:                  YES
after_t1_linear_bars:               13
after_t1_dates:                     ['2026-08-10','2026-08-11','2026-08-12','2026-08-13','2026-08-14',
                                     '2026-08-15','2026-08-16','2026-08-17','2026-08-18','2026-08-19',
                                     '2026-08-20','2026-08-21','2026-08-22']
linear_last:                        2026-08-22T00:00:00+00:00
linear_rows:                        1474
new_linear_bars_since_slice74:      1
new_funding_prints_since_slice74:   2      (funding through 2026-08-23T08:00:00Z)
the_window_grew:                    YES
2026-08-23_present_as_linear:       NO   (open bar, correctly absent)

funding_setups_in_window:           3 of 13   (08-19, 08-21, 08-22)
08-19_flag:                         YES
08-19_scoreable:                    NO
08-21_flag:                         YES   <-- NEW: no longer the last bar
08-21_scoreable:                    NO
08-22_close_join_rate:              0.00010000
08-22_setup:                        YES
08-22_flag:                         NO    (last bar)
remaining_closed_days_until_08_19_scoreable:   4      (from the file)
remaining_closed_days_until_08_21_scoreable:   6
remaining_closed_days_until_08_22_scoreable:   7
entries_taken:                      0
forward_n_trades:                   0
is_forward_observation:             false
ceiling_max_possible_trades:        8
within_ceiling:                     YES
forward_bars_with_a_barrier_outcome: 6
max_entries_the_schedule_would_admit: 2      (not 3 — see §58c)

oos_re_scored:                      NO
fund_abs_moved:                     NO   (0.0001)
join_unchanged:                     YES  (at_or_before bar CLOSE)
barrier_function_unchanged:         YES  (byte-identical to slice 74)
constants_fingerprint_match:        YES  662de0115880871352d5d623b1020eaa
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

suite:  baseline 8 failed / 4,767 passed / 2 skipped
        final    0 failed / 4,856 passed / 2 skipped
```

## The measurement

**Ceiling 8, setups 3 of 13, flagged 2, entries 0, closed trades 0.**

```
2026-08-10  0.00005057     2026-08-17  0.00009202
2026-08-11  0.00008282     2026-08-18  0.00003650
2026-08-12  0.00006601     2026-08-19  0.00010000   SETUP + FLAG
2026-08-13  0.00007841     2026-08-20  0.00009422
2026-08-14  0.00000629     2026-08-21  0.00010000   SETUP + FLAG  <-- new flag
2026-08-15  0.00005311     2026-08-22  0.00010000   SETUP (last bar)
2026-08-16  0.00005000     FUND_ABS    0.00010000
```

`2026-08-22` closed, so `2026-08-21` is no longer the corpus's last bar and it
**flags** — the transition `2026-08-19` made in slice 73. `2026-08-22` is itself
a **third setup**: its 16:00 print stands at the bar's close (`23:59:59.999Z`,
strictly before the `2026-08-23T00:00Z` print). Being last, it does not flag.

**Nothing is scoreable.** Eligibility reaches back seven bars, so with
`2026-08-22` last the highest scoreable bar is `2026-08-15`. Six of the thirteen
forward bars are scoreable; neither flag is among them.

## Three setups are at most two entries

`one_entry_per_contiguous_run` admits **one** entry per contiguous run of flags,
and the setups are not contiguous:

```
    08-19            isolated run           -> at most 1 entry
    08-20            not a setup            -> the gap that separates them
    08-21, 08-22     one contiguous run     -> at most 1 entry BETWEEN THEM
```

So even once every other gate opens the schedule admits **two**, not three. This
is the first slice where the distance between a setup count and a trade count is
more than one step, and the artefact now computes the runs rather than narrating
them.

**It is a bound on the schedule's output, not a prediction that either entry
happens.** Eligibility, the lockup and the caps all still stand in between. §55e
enumerated remaining gates and left one out; §56c recorded what that cost, and
this section is deliberately a bound rather than a list.

## Every gap recomputed, because the last one was wrong

```
    08-19   index 1470   needs last >= 1477 = 2026-08-26   4 closed days away
    08-21   index 1472   needs last >= 1479 = 2026-08-28   6
    08-22   index 1473   needs last >= 1480 = 2026-08-29   7
```

§57b said 08-19 was "four days short" when the last bar was `2026-08-21`; it was
**five**, and §57e owns that. It is four **now** — and the four is a subtraction
over indices read from the corpus this slice, not the earlier number reused
because it happens to match. The artefact carries the formula beside the numbers
and `test_the_gaps_are_recomputed_from_the_corpus` recomputes all three.

## The base rate is what is accumulating

All three setups sit at exactly `FUND_ABS`, and funding has now printed exactly
that value for six consecutive prints — 48 hours. §57c established that `0.0001`
is the venue's **base** rate, the modal value of this corpus, not a cap and not
an extreme. The run is unremarkable by the corpus's own standard:

```
    runs of consecutive prints at exactly base       294
    mean run length                                  4.0 prints
    longest                                          70 prints
    current trailing run                             6 prints  (~89th percentile)
```

**The setups are accumulating because the book is balanced.** That is §57c's
census made visible in the forward window — and it is precisely the moment at
which moving `FUND_ABS` would feel most reasonable and be most wrong.

**Nothing moved.** `FUND_ABS` 0.0001, `HORIZON` 5, join at bar close, caps,
schedule, and `barrier_r_for_all_bars` all unchanged and asserted.

## STEP 1 landed, again and deliberately

§58a–§58e were written, `grep`-ed and committed at `e10c312` **before a single
tool was derived** — verified on disk, at `HEAD`, and in the section list the
artefacts record. §56h is what happens when that is assumed instead of checked.

## Two small corrections

**The spelled-number table ran out.** `_SPELLED` mapped 5–12; the window reached
thirteen and the headline read "13 BARS" instead of "THIRTEEN". It **degraded
visibly rather than lying** — which is why a derived field is chosen — but a
lookup with a bounded domain is still a constant waiting to run out, and this one
ran out one bar after it was written. Extended to twenty, fallback kept so the
next overflow is loud.

**The whole-corpus setup pin now moves every time a forward bar flags.**
417 → 418 (slice 73) → 419 (here), because `2026-08-21` became a directed signal
bar. That is the pin working: **it must move by exactly one, for a nameable bar,
or something is wrong.** It stays an absolute — a join change would still fail
here with a number — and the recurrence is documented in the module rather than
added to an expiry list, because `test_funding_carry_fade_v1.py` predates the
mechanism and is never re-derived.

Of eight baseline failures, six were on a declared `EXPIRES_WITH_DATA` list
(four from slice 74, two from slice 73). The two that were not are exactly those
two pin tests. **Every cited test name was kept** — eight assertions amended,
none renamed, per slice 74's rule, and the check now covers both shipped
verdicts.

## Evidence table

| claim | observed | file / command |
|---|---|---|
| window reached 13 | `after_t1_linear` 12 → 13 | `test_thirteen_closed_bars_lie_after_t1` |
| growth is real | prefix digests match slice-57 pins | `test_history_is_untouched_and_growth_is_an_append` |
| 08-23 absent | clock-derived | `test_the_open_bar_is_absent` |
| funding not stale | +2 prints through 08-23T08:00Z | `test_the_funding_is_not_stale` |
| all 17 note claims true | scored from files | `test_every_note_claim_is_true` |
| finding says thirteen | "twelve" asserted absent | `test_the_finding_says_thirteen_and_not_twelve` |
| **ladder 3,2,0,0,0,0** | six states, separately | `test_the_state_ladder_reads_3_2_0_0_0_0` |
| **08-21 is now a flag** | and slice 74's record says it was not | `test_08_21_is_now_a_flag` |
| 08-22 is a third setup | from the corpus, not the artefact | `test_08_22_is_a_setup_reproduced_from_the_corpus` |
| it stands at the close, no flag | 23:59:59.999 < next 00:00; last bar | `test_08_22_stands_at_the_close_and_does_not_flag` |
| 08-20 still not a setup | close-join 0.00009422 | `test_08_20_is_still_not_a_setup` |
| no entry, no closed trade | candidates `[]`, entries 0 | `test_no_entry_and_no_closed_trade` |
| no rung read as a higher one | gate evidence says so | `test_a_lower_rung_is_never_reported_as_a_higher_one` |
| **runs: [08-19], [08-21, 08-22]** | computed, not narrated | `test_the_runs_are_08_19_alone_and_08_21_with_08_22` |
| **at most 2 entries, not 3** | schedule bound | `test_the_schedule_admits_at_most_two_not_three` |
| the 08-20 gap separates them | if it were a setup, one run | `test_the_gap_at_08_20_is_what_separates_them` |
| it is a bound, not a prediction | stated in EDGE | `test_it_is_a_bound_on_the_schedule_not_a_prediction` |
| eligibility reaches back 7 | highest scoreable 2026-08-15 | `test_eligibility_still_reaches_back_seven_bars` |
| measured from the frozen fn | both sides | `test_that_offset_is_measured_from_the_frozen_function` |
| **neither flag is scoreable** | absent from the index set | `test_neither_flag_has_a_barrier_outcome` |
| **gaps recomputed: 4, 6, 7** | from indices, this slice | `test_the_gaps_are_recomputed_from_the_corpus` |
| destinations 08-26/28/29 | derived from the last bar | `test_the_destinations_are_the_dates_the_note_names` |
| 6 forward bars scoreable | 08-10 … 08-15 | `test_six_forward_bars_are_scoreable_now` |
| the barrier fn untouched | source line unchanged | `test_the_frozen_function_was_not_modified` |
| **base-rate run is 6 prints** | 89th pct of 294 runs | `test_the_current_run_is_unremarkable` |
| the run census is measured | recomputed from the file | `test_the_run_census_is_measured_not_asserted` |
| all three setups at base | exactly `FUND_ABS` | `test_all_three_setups_sit_at_exactly_the_base_rate` |
| still modal, still not a cap | 282 above; max 8.8e-4 | `test_the_threshold_is_still_the_modal_value_and_not_a_cap` |
| **`FUND_ABS` did not move** | flags false; reason recorded | `test_fund_abs_did_not_move` |
| the temptation is named | §58d | `test_the_temptation_is_named_in_edge` |
| **ceiling = 8** | `max(0, 13 − 5)`, declared §58a | `test_the_ceiling_is_eight` |
| not 7, not 9 | both mission errors asserted | `test_it_is_not_seven_and_not_nine` |
| every prior ceiling lower | 0…7 | `test_every_prior_slice_ceiling_is_recorded_and_lower` |
| every prior numerator zero | from each frozen artefact | `test_every_prior_slice_observed_zero_closed_trades` |
| **pin moved 418 → 419** | independent of any artefact | `test_the_pin_is_now_419` |
| the mover is 08-21, by one | newest directed bar | `test_the_mover_is_08_21_and_it_moved_by_exactly_one` |
| the movement is now expected | documented in the module | `test_this_is_now_a_recurring_amendment_not_a_surprise` |
| the spelled table was extended | 13 present, max ≥ 20 | `test_the_spelled_number_table_no_longer_runs_out` |
| **STEP 1 landed** | §58a–e on disk, committed first | `test_edge_carries_58a_through_58e` |
| the artefacts recorded it | 58 in `sections_present` | `test_the_artefacts_recorded_58_as_present_when_they_ran` |
| the ceiling's citation resolves | §58a | `test_the_ceilings_citation_resolves` |
| guards 1–5 carried by import | re-run, not copied | `test_all_five_guards_still_exist_where_expected` |
| **no cited name was renamed** | both shipped verdicts checked | `test_a_cited_test_name_is_never_renamed` |
| scoring not re-tuned | 2 declared changes, 0 additions | `test_the_scoring_was_not_re_tuned_since_slice74` |
| expiry list matches the module | AST sweep | `test_the_declared_list_matches_the_module` |
| slice 74's list named all four | from its own module | `test_slice74s_list_named_all_four_of_its_failures` |
| the other two came from elsewhere | a module with no list | `test_the_pre_forward_module_has_no_such_list` |
| gate refuses | 2 / 8 | `test_the_gate_refuses` |
| OOS untouched | `28b7dfe0…`; folds `ff5cc8a2…` | `test_the_oos_clear_was_not_re_scored` |
| BTC only, 11 frozen | universe `("BTCUSDT",)` | `test_eleven_families_stay_frozen_and_the_universe_is_btc_only` |
| live dark | no `models/current` | `test_live_is_dark` |
| registration discipline | HELD | `artifacts/slice75_registration_discipline.log` |
| paper session | green | `artifacts/slice75_paper_cert.log` |
| suite | 4,856 passed / 0 failed / 2 skipped | `artifacts/slice75_pytest.log` |

## Forbidden list — every item, answered

| forbidden | this slice |
|---|---|
| invented bars | `bars_fabricated: 0` |
| 08-23 as closed linear | absent; clock-derived |
| false forward | `is_forward_observation: false`; ladder published |
| Stage-1 re-score | artefact byte-identical |
| `FUND_ABS` / join / caps / schedule / horizon change | all asserted unchanged |
| **barrier-fn change** | byte-identical to slice 74, asserted twice |
| live / `models/current` / training / Bybit | live dark |
| ETH/SOL expansion | universe `("BTCUSDT",)` |
| frozen reopen | 11 unchanged |
| exit clamping | false |
| autonomy YES | false |
| **claiming ceiling = 7 or 9 at N = 13** | asserted `!= 7` and `< 9` |
| counting flag/setup/entry as a closed trade | ladder; gate reads 0 of 20 |
| **claiming 08-19 is scoreable** | asserted absent from the index set |
| **still calling 08-21 "not a flag, last bar"** | it flags; the old claim moved to slice 74's frozen record |
| dropping guards | five imported and re-run |
| finding text still saying TWELVE | asserted absent |
| **moving `FUND_ABS` because it is the modal rate** | not moved; the temptation named in §58d |

## Carried forward

1. **Amend the eighteen `EXPIRES_WITH_DATA` tests** in
   `tests/test_slice75_thirteen_day_window.py`.
2. **08-19 becomes scoreable at `2026-08-26`** — four more closed bars from
   `2026-08-22`. Recompute from the file; do not reuse this number.
3. **08-21 flags now; 08-22 should flag next slice** if a bar closes after it.
4. **The setup pin will move again** whenever a forward bar stops being last.
   By exactly one, for a nameable bar.
5. **Four manifest entries remain frozen-wrong**; the funding seam at
   `2026-08-09T16:00Z` is still empty, fourteen slices on.

## What would move this forward

A flagged bar that is also **scoreable**, then a candidate, then a fill, then an
exit inside the corpus. Five gates; the pilot has passed two. The gate asks for
20 completed forward trades and 180 forward days; it has 0 and 13.

**Three setups, two flags, no entry. The numerator has never moved.**
