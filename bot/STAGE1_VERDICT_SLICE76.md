# SLICE 76 — VERDICT

```
SLICE76_VERDICT:                    PASS_WITH_DEFECTS
extension_present:                  YES
catch_up:                           YES  (two closed UTC days appended at once)
after_t1_linear_bars:               15
after_t1_dates:                     ['2026-08-10','2026-08-11','2026-08-12','2026-08-13','2026-08-14',
                                     '2026-08-15','2026-08-16','2026-08-17','2026-08-18','2026-08-19',
                                     '2026-08-20','2026-08-21','2026-08-22','2026-08-23','2026-08-24']
linear_last:                        2026-08-24T00:00:00+00:00
linear_rows:                        1476
new_linear_bars_since_slice75:      2      (2026-08-23 and 2026-08-24)
new_funding_prints_since_slice75:   7      (funding through 2026-08-25T16:00:00Z)
the_window_grew:                    YES
2026-08-25_present_as_linear:       NO   (open bar, correctly absent)

funding_setups_in_window:           5 of 15   (08-19, 08-21, 08-22, 08-23, 08-24)
08-19_flag:                         YES     08-19_scoreable: NO
08-21_flag:                         YES     08-21_scoreable: NO
08-22_flag:                         YES  <-- NEW: no longer the last bar
08-23_flag:                         YES  <-- NEW: promoted inside the same catch-up
08-24_close_join_rate:              0.00010000
08-24_setup:                        YES
08-24_flag:                         NO    (last bar)
remaining_closed_days_until_08_19_scoreable:   2      (from the file)
remaining_closed_days_until_08_21_scoreable:   4
remaining_closed_days_until_08_22_scoreable:   5
remaining_closed_days_until_08_23_scoreable:   6
remaining_closed_days_until_08_24_scoreable:   7
entries_taken:                      0
forward_n_trades:                   0
is_forward_observation:             false
ceiling_max_possible_trades:        10
within_ceiling:                     YES
forward_bars_with_a_barrier_outcome: 8
max_entries_the_schedule_would_admit: 2      (still 2 — the new setups extended an existing run)

state_ladder:                       5 / 4 / 0 / 0 / 0 / 0
                                    setups / flags / eligible / candidates / entries / CLOSED TRADES

oos_re_scored:                      NO   28b7dfe0f22c4867661bc73f6434c4acce54a0483c4469a6540155b1b3c18c51
folds_unmodified:                   YES  ff5cc8a2bb92362058b376659ff12f314c10f71a101d1f581f69da31f025013c
fund_abs_moved:                     NO   (0.0001)
join_unchanged:                     YES  (at_or_before bar CLOSE)
barrier_function_unchanged:         YES  (7-bar tail not shortened)
constants_fingerprint_match:        YES  662de0115880871352d5d623b1020eaa
caps_usd_100:                       YES  (1 position, 1 entry/day, $100, BTCUSDT)
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

suite:  baseline 8 failed / 4,848 passed / 2 skipped
        final    0 failed / 4,953 passed / 2 skipped
```

## The measurement

**Ceiling 10, setups 5 of 15, flagged 4, entries 0, closed trades 0.**

Two UTC days closed between packs because the human was late. That is the only
thing that happened. A catch-up is **two rows appended to one file**: it does
not license a second look at the rule, a wider universe, a new intake, or a
re-read of the clear.

The one thing two bars at once *does* change is the flag count. Each closed bar
makes the previous last bar no longer last, and a setup that is not the last bar
flags — so `2026-08-22` and `2026-08-23` were both promoted, and the count went
from two to four. **That is arithmetic about which bar is last, not momentum.**
Reading the doubling as acceleration is a reader supplying a thesis the data
does not carry. EDGE.md §59b.

`2026-08-24` is a fifth setup, close-joining at exactly `FUND_ABS` from its
16:00 print, and being the corpus's last bar it does not flag.

**Nothing is scoreable, and the catch-up did not change that.** A reader might
expect two extra bars to make an older flag scoreable. They do not by
themselves: `barrier_r_for_all_bars` blanks the last seven bars *measured from
the corpus's end*, so appending two moves the window forward by two **and the
tail forward by two**. What appending shrinks is the distance from a FIXED bar
to a moving boundary — `2026-08-19` went from four closed days away to two — and
only that shrinking eventually opens the gate. EDGE.md §59c.

## Five setups are STILL at most two entries

`one_entry_per_contiguous_run` admits one entry per contiguous run of flags.
`2026-08-19` is isolated; `2026-08-21` through `2026-08-24` are one run. So:

```
    slice 75    3 setups    2 runs    bound 2
    slice 76    5 setups    2 runs    bound 2
```

The setup count rose by two and the bound did not move at all, because the new
setups landed inside a run that already existed. This is the clearest
demonstration the pilot has produced that **counting setups was never counting
trades** — and the whole difference between a bound of one and a bound of two is
`2026-08-20`, which close-joined at `0.00009422` and missed by 5.8e-6. Nothing
was moved to rescue it.

## Every gap recomputed, none carried

```
    08-19   2 closed days away   -> 2026-08-26
    08-21   4                    -> 2026-08-28
    08-22   5                    -> 2026-08-29
    08-23   6                    -> 2026-08-30
    08-24   7                    -> 2026-08-31
```

Slice 74 said `08-19` was five closed days away, slice 75 said four, this slice
says two — and all three point at the same calendar date, `2026-08-26`. **The
destination is the durable claim; the distance is the expiring one**, and §57b
is on the record for reusing a distance and being wrong by one. Every number
above is `(i + HORIZON + 2) − last`, read from indices this slice.

## The predecessor digest, fixed at the instrument

Slice 75 wrote `slice74_linear_sha256_uncompressed` equal to its **own** current
digest, so `identical = true` and `differs = true` shipped in the same dict.
Nothing objected, because both digests were real and both were correctly
computed; the question no field asked was *whose* digest the constant was.

This slice:

* pins from slice 75's **CURRENT** values (`6e64847c…`, `fec0ee8c…`), never from
  that artefact's prior-digest field;
* **verifies** both constants against the previous artefact's own current fields
  before computing anything, and **raises** on a mismatch — fail closed;
* derives `identical` and `differs` from **one** comparison read with opposite
  senses, so no setting of the constant can make them agree, with a test
  forbidding the pair;
* runs a **full audit** of every prior-digest constant on the tree rather than
  repeating a count from prose.

The audit reads 20 constants across 16 artefacts and finds **four** defects —
slice 66 stale by two (matching neither its own digest nor its predecessor's)
and slices 73, 74 and 75 self-comparing. It **clears** slices 63 and 71, whose
equalities are correct: slice 71's funding file was RECOMPRESSED, not changed
(the §54d case), and slice 63's corpus genuinely did not grow. Reporting those
two as defects would have inflated the count; reporting the four as legitimate
would have hidden them. Each row names which comparison decided it. EDGE.md
§59d.

## `why_not_closer` — a field that was wrong for fourteen slices

Every gate artefact from slice 62 to slice 75 inclusive opened that field with
**"One post-t1 day"**, fourteen slices after there was one. It survived because
it was a hand-typed sentence in a prose field no instrument read.

It is now interpolated from this slice's own artefacts and carries a flag
saying so. **The earlier artefacts are not edited** — they are the record of
what was written when — and the residue is named in the new one instead.

## A rule that was true until it wasn't

Slice 75 wrote that the whole-corpus setup pin

> "moves by exactly one, for a nameable bar, or something is wrong."

That was true of slice 73 and true of slice 75, because each appended one bar.
It was never the rule. The pin moves by **the number of bars appended**: this
catch-up moved it `419 → 421`, with `2026-08-22` and `2026-08-23` as the two
nameable movers and `long_setups` unmoved at 6.

A sentence that has held for every slice so far is not thereby a rule; it is an
observation with a small sample. Hardening it into an absolute is how a correct
pin becomes a false alarm — or worse, how a later reader "fixes" a real count to
make a constant true. The pin did not fail here: it moved by exactly the amount
the append explains. EDGE.md §59f.

## Defects

**1. §59a miscounted the clean-pack run.** The pre-declared design note says
"Tenth consecutive clean pack". The run began at slice 68, so slice 76 is the
**NINTH**. The ceiling declaration in the same section — `max(0, 15 − 5) = 10` —
is correct and was not re-derived; the miscount is in adjacent prose about pack
provenance. It is corrected in the freshness artefact, which now derives
`consecutive_clean_packs` from `SLICE − 68 + 1` rather than carrying a word, and
records that §59a said otherwise. **§59a is not back-dated.** The same
correction applies to the pack-base claim's "held for eight slices running",
which is now nine, derived.

**2. Three cited test names in slice 75's module are now factually wrong** and
were kept anyway: `test_all_three_setups_sit_at_exactly_the_base_rate` (there
are five), `test_the_pin_is_now_419` (it is 421), and
`test_the_mover_is_08_21_and_it_moved_by_exactly_one` (it moved by two, and the
mover is 08-23). `STAGE1_VERDICT_SLICE75.md` cites all three and a shipped
verdict must not be made false by a rename (§57e). Each was amended in the body
with the correction written into its docstring. This is the rule working, not a
defect discovered — but a test whose name contradicts its assertions is a
liability a future reader will trip over, and it is named here rather than left
for them.

**3. Nine baseline assertions expired at once** — eight on slice 75's
`EXPIRES_WITH_DATA` list plus the whole-corpus pin in a module that predates the
mechanism. All eight declared ones were on the list, so the mechanism worked;
the ninth was not, for the same structural reason as last slice. The
pre-forward module still has no expiry list and is still never re-derived.

## Evidence table

| claim | observed | file / command |
| --- | --- | --- |
| window reached 15 | `after_t1_linear` 13 → 15 | `test_fifteen_closed_bars_lie_after_t1` |
| it was a catch-up, named as one | two rows, one file | `test_it_was_a_catch_up_and_the_artefact_says_so` |
| growth is real | prefix digests match slice-57 pins | `test_history_is_untouched_and_growth_is_an_append` |
| 08-25 absent | clock-derived | `test_the_open_bar_is_absent` |
| funding not stale | +7 prints through 08-25T16:00Z | `test_the_funding_is_not_stale` |
| both new bars' joins covered | rates at close; corpus extends past | `test_the_funding_covers_both_new_bars_close_joins` |
| all note claims true | scored from files | `test_every_note_claim_is_true` |
| finding says fifteen | "thirteen"/"fourteen" asserted absent | `test_the_finding_says_fifteen_and_not_thirteen` |
| **ladder 5,4,0,0,0,0** | six states, separately | `test_the_state_ladder_reads_5_4_0_0_0_0` |
| **flags moved by the bars appended** | 2 → 4 for +2 bars | `test_the_flag_count_moved_by_the_number_of_bars_appended` |
| **08-22 and 08-23 are now flags** | slice 75's record says they were not | `test_08_22_and_08_23_are_now_flags` |
| 08-24 is a fifth setup | from the corpus, not the artefact | `test_08_24_is_a_setup_reproduced_from_the_corpus` |
| it stands at the close, no flag | 23:59:59.999 < next 00:00; last bar | `test_08_24_stands_at_the_close_and_does_not_flag` |
| 08-20 still not a setup | close-join 0.00009422 | `test_08_20_is_still_not_a_setup` |
| no entry, no closed trade | candidates `[]`, entries 0 | `test_no_entry_and_no_closed_trade` |
| no rung read as a higher one | gate evidence says so | `test_a_lower_rung_is_never_reported_as_a_higher_one` |
| **runs: [08-19], [08-21…08-24]** | computed, not narrated | `test_the_runs_are_08_19_alone_and_08_21_through_08_24` |
| **at most 2 entries, not 5** | schedule bound | `test_the_schedule_admits_at_most_two_not_five` |
| **+2 setups did not raise the bound** | 2 runs before and after | `test_two_more_setups_did_not_raise_the_bound` |
| the 08-20 gap separates them | miss by 5.8e-6; nothing moved | `test_the_gap_at_08_20_is_what_separates_them` |
| it is a bound, not a prediction | stated in EDGE | `test_it_is_a_bound_on_the_schedule_not_a_prediction` |
| eligibility reaches back 7 | highest scoreable 2026-08-17 | `test_eligibility_still_reaches_back_seven_bars` |
| **the tail advanced with the window** | boundary +2, distance −2 | `test_the_tail_advanced_with_the_window_not_against_it` |
| measured from the frozen fn | both sides | `test_that_offset_is_measured_from_the_frozen_function` |
| **no flag is scoreable** | all four absent from the index set | `test_neither_flag_has_a_barrier_outcome` |
| **gaps recomputed: 2, 4, 5, 6, 7** | from indices, this slice | `test_the_gaps_are_recomputed_from_the_corpus` |
| destinations 08-26 … 08-31 | two last-bars, same dates | `test_the_destinations_are_the_dates_the_note_names` |
| 8 forward bars scoreable | 08-10 … 08-17 | `test_eight_forward_bars_are_scoreable_now` |
| the barrier fn untouched | source line unchanged | `test_the_frozen_function_was_not_modified` |
| the run census is measured | 295 runs, longest 70, trailing 2 | `test_the_run_census_is_measured_not_asserted` |
| **the base-rate run got SHORTER** | 6 → 2 prints, 89th → 71st pct | `test_the_current_run_got_SHORTER_and_that_is_reported` |
| all five setups at base | exactly `FUND_ABS`, none above | `test_all_five_setups_sit_at_exactly_the_base_rate` |
| census is this file's, prose agrees | 1,182 of 4,431; "1,174 of 4,422" absent | `test_the_census_is_this_slices_file_and_the_prose_agrees` |
| still modal, still not a cap | 282 above; max 8.8e-4 | `test_the_threshold_is_still_the_modal_value_and_not_a_cap` |
| **`FUND_ABS` did not move** | flags false; reason recorded | `test_fund_abs_did_not_move` |
| the temptation is named | §58d | `test_the_temptation_is_named_in_edge` |
| **ceiling = 10** | `max(0, 15 − 5)`, declared §59a | `test_the_ceiling_is_ten` |
| not 8, not 9, not 11 | catch-up appended TWO | `test_it_is_not_nine_and_not_eleven` |
| **ceiling moved by the bars appended** | 8 → 10; numerator still 0 | `test_the_ceiling_moved_by_the_number_of_bars_appended` |
| unconditional bound is commentary | 14 | `test_the_unconditional_bound_is_commentary_only` |
| every prior ceiling lower | 0…8 | `test_every_prior_slice_ceiling_is_recorded_and_lower` |
| every prior numerator zero | from each frozen artefact | `test_every_prior_slice_observed_zero_closed_trades` |
| **pin moved 419 → 421** | independent of any artefact | `test_the_pin_is_now_421` |
| the movers are 08-22 and 08-23 | by TWO, = bars appended | `test_the_movers_are_08_22_and_08_23_and_it_moved_by_two` |
| the step-size rule was corrected | in EDGE, not tidied away | `test_the_step_size_rule_was_corrected_not_quietly_fixed` |
| the spelled table covers 15 | max ≥ 20, fallback kept | `test_the_spelled_number_table_no_longer_runs_out` |
| **identical/differs cannot both hold** | one comparison, two senses | `test_identical_and_differs_cannot_both_be_true` |
| **slice 75 shipped that pair** | frozen artefact, unedited | `test_slice75_shipped_exactly_that_contradiction` |
| priors come from slice 75's CURRENT | not its prior-digest field | `test_the_prior_digests_come_from_slice75s_CURRENT_fields` |
| this slice does not self-compare | both flags false | `test_this_slices_constants_are_not_its_own_digests` |
| the tool fails closed on mismatch | `raise SystemExit` | `test_the_constants_are_verified_against_the_previous_artefact` |
| **audit: 4 defects, 2 legitimate** | 66/73/74/75 vs 63/71 | `test_the_audit_finds_the_four_and_clears_the_two` |
| **gate no longer says "One post-t1 day"** | derived from files | `test_the_gate_no_longer_says_one_post_t1_day` |
| every number in it is from artefacts | 15, 5, 4, 10, 6 of 8 | `test_every_number_in_it_comes_from_this_slices_artefacts` |
| the residue is named, not back-dated | slices 62–75 unedited | `test_the_residue_is_named_not_back_dated` |
| **STEP 1 landed** | §59a–f on disk, committed first | `test_edge_carries_59a_through_59f` |
| the artefacts recorded it | 59 in `sections_present` | `test_the_artefacts_recorded_59_as_present_when_they_ran` |
| the ceiling's citation resolves | §59a | `test_the_ceilings_citation_resolves` |
| guards 1–5 carried by import | re-run, not copied | `test_all_five_guards_still_exist_where_expected` |
| **no cited name was renamed** | shipped verdicts checked | `test_a_cited_test_name_is_never_renamed` |
| scoring not re-tuned | 2 declared changes, 0 additions | `test_the_scoring_was_not_re_tuned_since_slice75` |
| expiry list matches the module | AST sweep | `test_the_declared_list_matches_the_module` |
| gate refuses | 2 / 8 | `test_the_gate_refuses` |
| no human item completed in code | 0 forged | `test_no_human_item_was_completed_in_code` |
| OOS untouched | `28b7dfe0…`; folds `ff5cc8a2…` | `test_the_oos_clear_was_not_re_scored` |
| BTC only, 11 frozen | universe `("BTCUSDT",)` | `test_eleven_families_stay_frozen_and_the_universe_is_btc_only` |
| live dark | no `models/current` | `test_live_is_dark` |
| registration discipline | HELD | `artifacts/slice76_registration_discipline.log` |
| paper session | green | `artifacts/slice76_paper_cert.log` |
| suite | 4,953 passed / 0 failed / 2 skipped | `artifacts/slice76_pytest.log` |

## Forbidden list — every item, answered

| forbidden | this slice |
| --- | --- |
| invented bars | `bars_fabricated: 0` |
| **2026-08-25 as closed linear** | absent; clock-derived and asserted |
| false forward | `is_forward_observation: false`; ladder published |
| Stage-1 re-score | artefact byte-identical |
| `FUND_ABS` / join / caps / schedule / horizon change | all asserted unchanged |
| **shortening the 7-bar tail** | unchanged; asserted at source and by measurement |
| **renaming a test on the EXPIRES list** | none renamed; bodies amended, docstrings say so |
| live / `models/current` / training / Bybit | live dark |
| ETH/SOL expansion | universe `("BTCUSDT",)`; 4 manifest entries left frozen-wrong |
| frozen reopen | 11 unchanged |
| exit clamping | false |
| autonomy YES | false |
| **claiming ceiling ≠ 10 at N = 15** | asserted `!= 8`, `!= 9`, `< 11` |
| counting flag/setup/entry as a closed trade | ladder; gate reads 0 of 20 |
| **claiming any flag is scoreable** | all four asserted absent from the index set |
| **reading the doubled flag count as acceleration** | named as arithmetic in §59b, artefact and tests |
| **copying `slice74_linear_sha256_uncompressed` from slice 75** | refused; pinned from CURRENT and verified |
| dropping guards | five imported and re-run |
| human checklist items completed in code | 0 |

## Carried forward

1. **Amend the nineteen `EXPIRES_WITH_DATA` tests** in
   `tests/test_slice76_fifteen_day_window.py`. A catch-up expires two slices'
   worth at once; expect more than one.
2. **08-19 becomes scoreable at `2026-08-26`** — two more closed bars from
   `2026-08-24`. **Recompute from the file; do not reuse this number.**
3. **08-24 should flag next slice** if a bar closes after it. The flag count
   rises by the number of bars appended, not by one.
4. **The setup pin will move again**, by the number of bars appended, for
   nameable bars.
5. **Four manifest entries remain frozen-wrong**; the funding seam at
   `2026-08-09T16:00Z` is still empty, sixteen slices on.
6. **§59a's "tenth consecutive clean pack" is off by one.** The derived count
   in the freshness artefact is the one to trust.

## What would move this forward

A flagged bar that is also **scoreable**, then a candidate, then a fill, then an
exit inside the corpus. Five gates; the pilot has passed two. The gate asks for
20 completed forward trades and 180 forward days; it has **0 and 15**.

**Five setups, four flags, no entry. The numerator has never moved.**
