# SLICE 70 — VERDICT

```
SLICE70_VERDICT:                    PASS_WITH_DEFECTS
extension_present:                  YES
after_t1_linear_bars:               8
after_t1_dates:                     ['2026-08-10','2026-08-11','2026-08-12','2026-08-13',
                                     '2026-08-14','2026-08-15','2026-08-16','2026-08-17']
linear_last:                        2026-08-17T00:00:00+00:00
linear_rows:                        1469
new_linear_bars_since_slice69:      1
the_window_grew:                    YES
2026-08-18_present:                 NO   (open bar, correctly absent)

forward_n_trades:                   0
forward_observations_to_date:       0
is_forward_observation:             false
ceiling_max_possible_trades:        3
within_ceiling:                     YES
funding_setups_in_window:           0 of 8
08-17_close_join_rate:              0.00009202

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

suite:  baseline 2 failed / 4,437 passed / 2 skipped
        final    0 failed / 4,500 passed / 2 skipped
```

## The measurement

**Ceiling 3, setups 0 of 8, fills 0.**

```
2026-08-10  0.00005057     2026-08-14  0.00000629
2026-08-11  0.00008282     2026-08-15  0.00005311
2026-08-12  0.00006601     2026-08-16  0.00005000
2026-08-13  0.00007841     2026-08-17  0.00009202
                           FUND_ABS    0.00010000
```

Every rate standing at a decision is below the threshold. The newest is the
closest yet at a shortfall of `8e-6`.

There is no second sentence entitled to more. It says the funding regime over
2026-08-10…17 never reached `FUND_ABS` at a decision. It says nothing about
whether the rule makes money: no position was opened, so only **selectivity**
has been exercised, never skill.

**The denominator went 0 → 1 → 2 → 3 over four slices. The numerator has never
moved off 0.**

## Two near misses now exist, and between them they exhaust the rescues

```
2026-08-12T00:00Z   0.00010000   == FUND_ABS exactly, then superseded
                                    by 08:00 = 8.568e-5 and 16:00 = 6.601e-5
                                    -> only a JOIN change could capture it

2026-08-17T16:00Z   0.00009202   stood AT the bar's close and still missed
                                    -> only a lower FUND_ABS could capture it
```

The second is the more instructive. No join rule would have rescued it, because
the join already gave it the rate it asked for. **The only available rescue is
the threshold, and the threshold is the clear.** `FUND_ABS` stays `0.0001`, the
horizon stays 5, the join stays `at_or_before(close_time_ms(bar))`, and both
artefacts carry `fund_abs_moved_this_slice: false` and
`join_changed_this_slice: false` with both dates cited in `why_neither_moved`.

A near miss makes the temptation larger, not the change more defensible.

## Why `PASS_WITH_DEFECTS`

Every PASS condition is met, the pack arrived intact for the third slice
running, all eleven note claims verify against files, and the ceiling is
correctly stated as 3. The downgrade is for defects **in my own prior work**,
found at this slice's baseline and disclosed here rather than quietly repaired:

1. A guard that four verdicts depended on **has not existed since slice 66**.
2. **Slice 67's verdict cited it by name** as evidence that it had run.
3. Two of my slice-69 tests failed because they pinned live data to literals.

A clean PASS would hide all three. The measurement itself is not tainted by any
of them, and that is verified below rather than asserted.

## The defect: a guard that was gone, and a verdict that said it ran

`test_the_scoring_was_not_re_tuned_since_sliceNN` is the AST comparison that
makes silent re-tuning of the forward-shadow scoring impossible. It existed in
slices 63–66. **It is absent from slices 67, 68 and 69** — each slice's test
file is written fresh rather than inherited, and it simply stopped being
carried.

Slice 67's evidence table says otherwise:

> `| scoring not re-tuned | AST-equal per function vs slice 66 | test_the_scoring_was_not_re_tuned_since_slice66 |`  <!-- known-missing: exhibited, not cited -->

**That row names a test that was not in the tree it shipped with.** An evidence
table is where a claim gets tied to the thing that checks it, so a row citing a
missing test reads as a *stronger* guarantee than no row would. This is the same
class as slice 67's false human-note claim, and it is mine.

**The property held, and that is checked rather than assumed.** The comparison
was re-run backwards across every slice it was missing from:

```
66 -> 67   build body AST-identical   no key dropped
67 -> 68   build body AST-identical   no key dropped
68 -> 69   build body AST-identical   no key dropped   (+5 keys, mission-asked)
69 -> 70   build body AST-identical   no key dropped   (+1 key, declared)
```

Nothing was re-tuned while the guard was away. **The absence is the defect; the
scoring was not touched.** Both halves are stated, in that order, and neither
swallows the other.

## Two more members for the guard family

The fix for a recurring defect is never the instance. It is now four for four:

| # | slice | guard | catches |
|---|---|---|---|
| 1 | 66 | `TestNoTestPinsALiveAbsolute` | live count `==` integer literal |
| 2 | 69 | `TestNoToolCarriesAnotherSlicesConstant` | a tool naming a foreign slice |
| 3 | **70** | `TestNoTestEqualsALiveCountAgainstADatedArtefact` | live count `==` **dated artefact** |
| 4 | **70** | `TestAVerdictMayNotCiteATestThatDoesNotExist` | a citation with nothing behind it |

**Member three** exists because member one could not see this slice's second
baseline failure: `cp.check(...).rows_on_disk == load(FRESHNESS)["linear_rows"]`
contains no integer literal — the right side is a lookup into a **dated**
record. The prefix invariant says a corpus may grow and may never shrink, so
equality against a past date asserts the invariant's opposite and holds only
until the next bar. Against a dated artefact the correct relation is `>=`;
against **this slice's own** artefact equality is right, and the guard tells
them apart by resolving the artefact path's slice number.

**Member four** exists because no sweep over code can catch a claim about code
that isn't there. It parses every `test_*` name out of this slice's verdict —
including this table — and fails if any is missing from the test tree. Scoped to
the current verdict on purpose: slice 67's is a historical record and must not
be edited to make a later guard pass.

It fired on its first run, on **this document**: the block quote above exhibits
slice 67's missing name. A quoted absence is legitimate and a citation is not,
so that line carries a `known-missing` marker — and the marker is not a
loophole, because a marked name that turns out to **exist** fails just as hard.
The same shape as slice 69's `prior-slice` annotation: **a deliberate reference
is a decision and must be marked; an accidental one is a defect.**

## The stale literal, and the rule it produced

`test_the_open_bar_is_absent` (slice 69) asserted twice: once from the clock,
once from a hard-coded `2026-08-17`. The docstring said it could not expire. The
line below it expired the moment 08-17 closed — the exact event this programme
is waiting for.

> **When a check can be derived from the clock or the data, derive it and stop.
> A literal added beside a derived check is not corroboration; it is the only
> line that can go stale, and it will.**

Applied beyond the repair: this slice's findings, ceiling notes and window notes
now interpolate their counts, including the spelled-out bar word, so no artefact
can go on saying "seven" into a slice where the window is eight.

## A third stale narrative, found the same way

`_manifest_audit`'s finding text asserted **"The manifests were updated this
slice by BROADCASTING BTCUSDT's new row counts…"**. The broadcast stopped in
slice 67. The sentence shipped in 68, 69 and this slice's inherited tool —
present tense, about a past slice, and quoting `4392` for a manifest that says
`4397`. It is now **derived** from the audit's own numbers, with a computed
`broadcast_occurred_this_slice: false` beside it, and a test asserts the old
sentence cannot come back.

## Evidence table

| claim | observed | file / command |
|---|---|---|
| window reached 8 | `after_t1_linear` 7 → 8 | `artifacts/slice70_data_freshness.json` |
| growth is real | prefix digests match slice-57 pins; append-only | `test_history_is_untouched_and_growth_is_an_append` |
| only BTC grew | ETH/SOL appended 0 rows | `test_only_btc_grew` |
| 08-18 absent | clock-derived, no literal beside it | `test_the_open_bar_is_absent` |
| all 11 note claims true | scored from files | `test_every_note_claim_is_true` |
| note path names slice 70 | derived from `SLICE`, and `SLICE == 70` | `test_the_note_path_names_this_slice_and_is_derived` |
| **ceiling = 3** | `max(0, 8 − 5)`, declared §53a before the run | `test_the_ceiling_is_three` |
| not 2, not 4 | both mission errors asserted against | `test_it_is_not_two_and_not_four` |
| every prior ceiling lower | 0,0,0,0,0,0,1,2 from frozen artefacts | `test_every_prior_slice_ceiling_is_recorded_and_lower` |
| 0 setups of 8 | every decision rate below `FUND_ABS` | `test_zero_setups_on_all_eight_bars` |
| per-bar table, all 8 dates | date, rate, setup flag, shortfall | `test_the_per_bar_table_covers_every_date` |
| 08-17 = 0.00009202 at the close | stood at the decision and still missed | `test_the_08_17_print_stood_at_the_close_and_still_missed` |
| 08-12 still the only threshold touch | superseded before its close | `test_the_08_12_print_still_equals_fund_abs_and_still_missed` |
| `FUND_ABS` unchanged | 0.0001; flag `false` | `test_fund_abs_is_unchanged` |
| join unchanged | both decision rates reproduced from the corpus | `test_the_join_is_unchanged` |
| both rescues refused | `why_neither_moved` cites both dates | `test_the_reason_neither_moved_cites_both` |
| finding says eight | "six"/"seven" asserted absent | `test_the_finding_says_eight_and_not_six_or_seven` |
| scoring not re-tuned | AST-equal; 4 declared changes, 1 declared addition | `test_the_scoring_was_not_re_tuned_since_slice69` |
| **no drift while the guard was gone** | 66→70, body identical, no key dropped | `test_the_scoring_did_not_drift_while_the_guard_was_absent` |
| the absence spanned 3 slices | 67, 68, 69 carry no such test | `test_the_absence_spanned_four_slices_and_is_recorded` |
| slice 67's verdict cited a missing test | that name is absent from every test module | `test_slice67s_verdict_cited_a_test_that_did_not_exist` |
| new guard 3 works | 4 control cases incl. the exact defect | `test_the_guard_catches_the_defect_it_was_written_for` |
| tree is clean under guard 3 | full sweep, 0 offences | `test_no_test_in_the_tree_equates_a_live_count_to_a_dated_record` |
| new guard 4 works | would have failed slice 67's row | `test_the_guard_would_have_caught_slice67s_row` |
| this verdict cites nothing missing | every `test_*` name resolves | `test_every_test_this_verdict_cites_exists` |
| amendments did not weaken 69 | dated claims checked vs frozen artefact | `test_the_amendments_did_not_weaken_the_frozen_claim` |
| manifest finding is derived | old broadcast sentence asserted gone | `test_the_manifest_finding_is_derived_not_narrated` |
| broadcast still stopped | only BTC entries moved | `test_the_broadcast_is_still_stopped_and_the_residue_still_there` |
| meta-guards 1 and 2 hold | AST sweeps clean | `test_the_meta_guards_are_all_enforced` |
| no restore needed | artefact absent; third clean pack | `test_no_restoration_was_needed_again` |
| pack-base claim true | EDGE §45a–§52a in the delivered tree | `test_the_pack_base_claim_is_true_this_time` |
| no edge measurement ran | artefacts carry no percentile/replicates | `test_no_edge_measurement_ran_this_slice` |
| gate refuses | 2 / 8, `allows_live` false | `test_the_gate_refuses` |
| gate refuses 3 possible trades | evidence says so in words | `test_the_gate_refuses_to_credit_three_possible_trades` |
| OOS untouched | `28b7dfe0…`; folds `ff5cc8a2…` | `test_the_oos_clear_was_not_re_scored` |
| live dark | no `models/current`, `policy_mode` off | `test_live_is_dark` |
| registration discipline | HELD | `artifacts/slice70_registration_discipline.log` |
| paper session | green | `artifacts/slice70_paper_cert.log` |
| suite | 4,500 passed / 0 failed / 2 skipped | `artifacts/slice70_pytest.log` |

## Forbidden list — every item, answered

| forbidden | this slice |
|---|---|
| synthetic / invented / forward-filled bars | `bars_fabricated: 0` |
| including 2026-08-18 as closed | absent; clock-derived assertion |
| relabelling bars ≤ `t1` as forward | strict at `t1` |
| trusting the note without verifying | 11 claims scored from files |
| re-scoring OOS / Stage-1 | artefact byte-identical |
| changing constants, schedule, caps, monitors | fingerprint `662de011…` |
| rescuing 08-12 or 08-17 via `FUND_ABS` or the join | neither moved; both flags `false`, both dates cited |
| raising `max_notional_usd` above 100 | 100.00 |
| arming live, `models/current`, training, Colab, GPUs | live dark; no weights in tree |
| reopening frozen families / extra coins | 11 unchanged; universe is `("BTCUSDT",)` |
| `closer_to_autonomous_profit_agent = true` | false |
| shadow mean R as Stage-1 evidence | `forward_mean_net_r: null` |
| completing human checklist items in code | 0 |
| clamping unfinished exits | false |
| claiming growth when `after_t1_linear ≠ 8` | it is 8, from disk |
| **claiming ceiling = 2 at N = 8** | asserted `!= 2` |
| **claiming ceiling ≥ 4 at N = 8** | asserted `< 4` |
| finding text still saying "six" or "seven" | asserted absent in three findings |
| stale `human_note_path` (SLICE64 / 69) | `…SLICE70.md`, derived, and `SLICE == 70` asserted |
| a narrative that eight quiet days prove or disprove the clear | stated as ceiling / setups / fills, and stopped |

## Carried forward

1. **Four manifest entries remain frozen-wrong.** ETH/SOL linear should read
   1461; ETH funding 4383; SOL funding 4458. Still residue, still not edited —
   rewriting a provenance record to agree with disk certifies nothing.
2. **The funding seam at `2026-08-09T16:00Z` is still empty**, nine slices on.
3. **Carry the guards forward, or the guard family is decorative.** Slices 67–69
   dropped one silently. Members three and four are in
   `tests/test_slice70_eight_day_window.py`; a slice-71 test module that does not
   import or re-run them re-opens exactly this hole.

## What would move this forward

A funding rate **at or above `1e-4` standing at a bar's close**, then a barrier
resolving inside the window. The window is long enough and getting longer; the
regime is not obliging. The gate asks for 20 completed forward trades and 180
forward days; the pilot has 0 and 8.

**The ceiling is a denominator. It grew because a day closed — not because the
rule, the data, or the regime got better.**
