# SLICE 74 — VERDICT

```
SLICE74_VERDICT:                    PASS
extension_present:                  YES
after_t1_linear_bars:               12
after_t1_dates:                     ['2026-08-10','2026-08-11','2026-08-12','2026-08-13','2026-08-14','2026-08-15',
                                     '2026-08-16','2026-08-17','2026-08-18','2026-08-19','2026-08-20','2026-08-21']
linear_last:                        2026-08-21T00:00:00+00:00
linear_rows:                        1473
new_linear_bars_since_slice73:      1
new_funding_prints_since_slice73:   3      (funding through 2026-08-22T16:00:00Z)
the_window_grew:                    YES
2026-08-22_present_as_linear:       NO   (open bar, correctly absent)

funding_setups_in_window:           2 of 12      (2026-08-19, 2026-08-21)
08-19_flag:                         YES
08-19_scoreable:                    NO    (needs last >= 2026-08-26 — five bars away)
08-21_close_join_rate:              0.00010000
08-21_setup:                        YES   SHORT; last bar, so not a flag
entries_taken:                      0
forward_n_trades:                   0
is_forward_observation:             false
ceiling_max_possible_trades:        7
within_ceiling:                     YES
forward_bars_with_a_barrier_outcome: 5

oos_evidence_re_scored:             NO
shadow_schedule_one_per_run:        YES
constants_fingerprint_match:        YES  662de0115880871352d5d623b1020eaa
fund_abs_unchanged:                 YES  (0.0001)
join_unchanged:                     YES  (at_or_before bar CLOSE)
barrier_function_unchanged:         YES  (byte-identical to slice 73)
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

suite:  baseline 5 failed / 4,695 passed / 2 skipped
        final    0 failed / 4,775 passed / 2 skipped
```

## The measurement

**Ceiling 7, setups 2 of 12, flagged 1, entries 0, closed trades 0.**

```
2026-08-10  0.00005057     2026-08-16  0.00005000
2026-08-11  0.00008282     2026-08-17  0.00009202
2026-08-12  0.00006601     2026-08-18  0.00003650
2026-08-13  0.00007841     2026-08-19  0.00010000   SETUP + FLAG (not scoreable)
2026-08-14  0.00000629     2026-08-20  0.00009422
2026-08-15  0.00005311     2026-08-21  0.00010000   SETUP (last bar → no flag)
                           FUND_ABS    0.00010000
```

`2026-08-21`'s 16:00 print stands at the bar's close — `23:59:59.999Z`, strictly
before the `2026-08-22T00:00Z` print — so it is the **second forward setup**. It
is the last bar, so `directed_signal_bars` excludes it: the same structure
`2026-08-19` was in one slice ago.

**The ladder: 2 / 1 / 0 / 0 / 0 / 0.** Setups, flags, eligible-and-flagged,
candidates, entries, closed trades. The gate counts only the last, and it reads
0 of 20.

## STEP 1 landed this time

§56h recorded a design note that never reached the repository. §57a–§57d were
written, `grep`-ed, and committed at `6df2a75` **before a single tool was
derived** — verified three ways: on disk, at `HEAD`, and in the section list the
artefacts themselves record. Guard five confirms every cited section was present
when each artefact ran.

## The threshold sits on the venue's base rate

The forward window has now produced three prints at exactly `FUND_ABS` and the
newest four prints in the file are all `0.00010000`. That reads like a regime
turning extreme. It is not.

```
prints                       4,422
exactly 0.00010000           1,174    26.6%   <- the modal value
strictly above               282       6.4%   max 0.00088148
strictly below -0.0001        24       0.5%   min -0.00119172
runner-up value count             4
pre-t1 share at 1e-4          26.6%           (not a forward phenomenon)
```

**`0.0001` is not a cap** — 282 prints exceed it. It is the venue's **base
funding rate**, the value the rate sits at when the book is balanced, and the
modal value of this corpus by nearly three hundred to one over the runner-up.

`FUND_ABS = 0.0001` with an inclusive `>=` therefore selects **base-or-above**
funding, not extreme funding. The arithmetic is consistent end to end: ~26% of
days qualifying at the decision gives the ~418 setup days `summary()` reports,
which `one_entry_per_contiguous_run` collapses into the **41 flag runs** the OOS
clear was measured on.

**Three things this does not license.**

1. **It does not invalidate the clear.** The rotation null was scored by
   identical arithmetic on the same threshold, so whatever the threshold admits,
   it admitted for both sides. That is the entire point of scoring both sides
   with one function.
2. **It does not license moving `FUND_ABS`.** This is the most plausible version
   of that temptation the programme has produced, because **it arrives dressed
   as a correction rather than an optimisation**. A threshold changed after
   seeing which prints qualify is fitted to data either way.
3. **It does not make the forward setups artefacts.** 08-19 and 08-21 are
   ordinary qualifying prints under an unchanged rule.

**What it does change is the programme's own language.** Earlier sections
describe the trigger as extreme funding. That is wrong: the rule fades
base-or-above funding, and its selectivity comes from the close-time join and
the contiguous-run schedule. Nothing measured moves; a description does.

## The flag is still not scoreable, and the subtraction was wrong

`2026-08-19` remains flagged and remains unscoreable. Eligibility reaches back
seven bars from the corpus end (§56d), so with `2026-08-21` last, the highest
scoreable bar is `2026-08-14`. Five of the twelve forward bars are scoreable.

**§57b said 08-19 was "four days short, by subtraction rather than forecast".
The corpus says five** — 08-22, 23, 24, 25, 26. The destination was right, the
distance was not.

§57b is left as written; back-editing a pre-declaration destroys the only thing
that makes one worth anything. The correction is in §57e and in
`test_it_needs_the_corpus_to_reach_08_26`, which computes the gap from the file
and asserts 5.

The sentence claimed its own rigour and then got the arithmetic wrong. **A claim
about method is not evidence of method** — the check is running the subtraction
against the file.

## Guard five had two defects of its own, one slice old

Both surfaced at this slice's baseline, and neither was on slice 73's
`EXPIRES_WITH_DATA` list.

**It expired undeclared.**
`test_the_recorded_digest_matches_the_edge_that_ships_beside_it` compared the
artefact's recorded `EDGE.md` digest to the live file — right within its own
slice, wrong the moment the next slice appends a section, which every slice
does. The list missed it because the sweep looks for **corpus** reads and this
reads a **document**. `LIVE_READS` is widened; the durable half (the recorded
section list, which is what the citation check actually uses) is what is now
asserted.

**It crashed rather than failed.**
`test_from_git_when_a_repository_is_present` was gated on "a repository is
present". Slice 74's tree is a *new* repository built from the unzipped
deliverable, so `.git` exists and slice 73's commit does not — `git show` exited
non-zero instead of the test reporting anything. **A guard that errors on a
legitimate state is not a guard.** The condition is now whether the artefact's
own commit is reachable, and a companion test reports whether the git form could
run at all, so "it passed" and "it had nothing to check" cannot be confused.

Once a slice ships, its history is gone by construction. The git form's domain
is the slice that wrote the artefact; the self-reported record survives beyond
it. **That is a domain, not a skip — and only honest because the other check
always runs.**

## A cited test name is part of the record

Renaming that test to describe its new condition made guard four fail:
`STAGE1_VERDICT_SLICE73.md` cites it, that verdict is shipped, and a record
naming a test that no longer exists is false. The rename was reverted, for the
same reason slice 70 refused to edit slice 67's verdict.

**Amend the body, never the name.** The name now under-describes what the test
does, which is the cheaper of the two errors, and
`test_a_cited_test_name_is_never_renamed` enforces it against slice 73's verdict.

## Evidence table

| claim | observed | file / command |
|---|---|---|
| window reached 12 | `after_t1_linear` 11 → 12 | `test_twelve_closed_bars_lie_after_t1` |
| growth is real | prefix digests match slice-57 pins | `test_history_is_untouched_and_growth_is_an_append` |
| 08-22 absent | clock-derived | `test_the_open_bar_is_absent` |
| funding not stale | +3 prints through 08-22T16:00Z | `test_the_funding_is_not_stale` |
| all 16 note claims true | scored from files | `test_every_note_claim_is_true` |
| finding says twelve | "eleven" asserted absent | `test_the_finding_says_twelve_and_not_eleven` |
| **ladder 2,1,0,0,0,0** | six states, separately | `test_the_state_ladder_reads_2_1_0_0_0_0` |
| **second setup: 08-21** | from the corpus, not the artefact | `test_08_21_is_reproduced_from_the_corpus_not_the_artefact` |
| it stands at the close | 23:59:59.999 < next 00:00 | `test_the_qualifying_print_stands_at_the_close` |
| it is not a flag | last bar; excluded by the rule | `test_08_21_is_not_a_flag_because_it_is_the_last_bar` |
| 08-20 still not a setup | close-join 0.00009422 | `test_08_20_is_still_not_a_setup` |
| no entry, no closed trade | candidates `[]`, entries 0 | `test_no_entry_and_no_closed_trade` |
| no rung read as a higher one | gate evidence says so | `test_a_lower_rung_is_never_reported_as_a_higher_one` |
| **08-19 still not scoreable** | eligibility reaches back 7 | `test_eligibility_still_reaches_back_seven_bars` |
| measured from the frozen fn | both sides | `test_that_offset_is_measured_from_the_frozen_function` |
| it has no barrier outcome | absent from the index set | `test_08_19_has_no_barrier_outcome` |
| **the gap is 5, not 4** | computed from the corpus | `test_it_needs_the_corpus_to_reach_08_26` |
| 5 forward bars scoreable | 08-10 … 08-14 | `test_five_forward_bars_are_scoreable_now` |
| the barrier fn untouched | source line unchanged | `test_the_frozen_function_was_not_modified` |
| **1e-4 is the modal print** | 1,174 of 4,422; runner-up 4 | `test_it_is_the_modal_value_by_a_wide_margin` |
| it is not a cap | 282 above; max 8.8e-4 | `test_it_is_not_a_cap` |
| the census is measured | recomputed from the file | `test_the_census_is_measured_not_asserted` |
| the pattern predates t1 | same share pre-t1 | `test_the_pattern_predates_t1` |
| both setups are base-rate prints | exactly at `FUND_ABS` | `test_the_forward_setups_are_ordinary_qualifying_prints` |
| language corrected, no number moved | asserted in the artefact | `test_this_corrects_language_and_moves_no_number` |
| `FUND_ABS` did not move | flags false; reason recorded | `test_fund_abs_did_not_move` |
| the correction is in EDGE | §57c | `test_the_correction_is_recorded_in_edge` |
| **ceiling = 7** | `max(0, 12 − 5)`, declared §57a | `test_the_ceiling_is_seven` |
| not 6, not 8 | both mission errors asserted | `test_it_is_not_six_and_not_eight` |
| every prior ceiling lower | 0…6 | `test_every_prior_slice_ceiling_is_recorded_and_lower` |
| every prior numerator zero | from each frozen artefact | `test_every_prior_slice_observed_zero_closed_trades` |
| **STEP 1 landed** | §57a–d on disk, committed first | `test_edge_carries_57a_through_57d` |
| the artefacts recorded it | 57 in `sections_present` | `test_the_artefacts_recorded_57_as_present_when_they_ran` |
| the ceiling's citation resolves | §57a | `test_the_ceilings_citation_resolves` |
| §56h is still on the record | not quietly dropped | `test_slice73s_artefacts_did_not_record_56` |
| guards 1–5 carried by import | re-run, not copied | `test_all_five_guards_still_exist_where_expected` |
| **a cited name is never renamed** | vs slice 73's verdict | `test_a_cited_test_name_is_never_renamed` |
| scoring not re-tuned | 2 declared changes, 0 additions | `test_the_scoring_was_not_re_tuned_since_slice73` |
| expiry list matches the module | AST sweep | `test_the_declared_list_matches_the_module` |
| slice 73's list named its 3 | from its own module | `test_slice73s_list_named_the_three_corpus_failures` |
| **and missed 2, and why** | they read a document | `test_the_two_it_missed_read_a_document_not_the_corpus` |
| gate refuses | 2 / 8 | `test_the_gate_refuses` |
| OOS untouched | `28b7dfe0…`; folds `ff5cc8a2…` | `test_the_oos_clear_was_not_re_scored` |
| BTC only, 11 frozen | universe `("BTCUSDT",)` | `test_eleven_families_stay_frozen_and_the_universe_is_btc_only` |
| live dark | no `models/current` | `test_live_is_dark` |
| registration discipline | HELD | `artifacts/slice74_registration_discipline.log` |
| paper session | green | `artifacts/slice74_paper_cert.log` |
| suite | 4,775 passed / 0 failed / 2 skipped | `artifacts/slice74_pytest.log` |

## Forbidden list — every item, answered

| forbidden | this slice |
|---|---|
| invented bars | `bars_fabricated: 0` |
| 08-22 as closed linear | absent; clock-derived |
| false forward | `is_forward_observation: false`; ladder published |
| Stage-1 re-score | artefact byte-identical |
| `FUND_ABS` / join / caps / schedule / horizon change | all asserted unchanged |
| **barrier-fn change** | byte-identical to slice 73, asserted twice |
| live / `models/current` / training / Bybit | live dark |
| ETH/SOL expansion | universe `("BTCUSDT",)` |
| frozen reopen | 11 unchanged |
| exit clamping | false |
| autonomy YES | false |
| **claiming ceiling = 6 or 8 at N = 12** | asserted `!= 6` and `< 8` |
| counting flag/setup/entry as a closed trade | ladder; gate reads 0 of 20 |
| **claiming 08-19 is scoreable** | asserted absent from the index set |
| dropping guards | five imported and re-run |
| finding text still saying ELEVEN | asserted absent |

## Carried forward

1. **Amend the fifteen `EXPIRES_WITH_DATA` tests** in
   `tests/test_slice74_twelve_day_window.py` — the list is in the module and
   the sweep now covers document reads as well as corpus reads.
2. **08-19 becomes scoreable when the corpus reaches `2026-08-26`** — five more
   closed bars from `2026-08-21`. Check that against the file, not against this
   line.
3. **Do STEP 1 in the repository and verify it landed.** It worked this slice
   because it was grepped, not assumed.
4. **Four manifest entries remain frozen-wrong**; the funding seam at
   `2026-08-09T16:00Z` is still empty, thirteen slices on.

## What would move this forward

A flagged bar that is also **scoreable**, then a candidate, then a fill, then an
exit inside the corpus. Five gates; the pilot has passed two. The gate asks for
20 completed forward trades and 180 forward days; it has 0 and 12.

**Two setups and one flag are not a trade. The numerator has never moved.**
