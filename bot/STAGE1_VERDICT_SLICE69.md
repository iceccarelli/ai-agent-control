# SLICE 69 — VERDICT

```
SLICE69_VERDICT:                    PASS
extension_present:                  YES
after_t1_linear_bars:               7
after_t1_dates:                     ['2026-08-10','2026-08-11','2026-08-12','2026-08-13',
                                     '2026-08-14','2026-08-15','2026-08-16']
linear_last:                        2026-08-16T00:00:00+00:00
linear_rows:                        1468
new_linear_bars_since_slice68:      1
the_window_grew:                    YES

forward_n_trades:                   0
forward_observations_to_date:       0
is_forward_observation:             false
ceiling_max_possible_trades:        2
within_ceiling:                     YES
funding_setups_in_window:           0 of 7

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
ProjectStatus.cleared_edge_signal:  funding_carry_fade_btc_v1
Closer to autonomous profit agent?: NO

suite:  baseline 1 failed / 4,388 passed / 2 skipped
        final    0 failed / 4,439 passed / 2 skipped
```

## The measurement

**Ceiling 2, setups 0 of 7, fills 0.**

```
2026-08-10  0.00005057     2026-08-14  0.00000629
2026-08-11  0.00008282     2026-08-15  0.00005311
2026-08-12  0.00006601     2026-08-16  0.00005000
2026-08-13  0.00007841     FUND_ABS    0.00010000
```

Every rate at a decision sits below the threshold; the newest is half of it. The
`2026-08-12T00:00:00Z` print remains the only forward print ever to touch the
line, and remains superseded before its bar's close.

`FUND_ABS` and the join are unchanged, now recorded as explicit `false` flags
rather than left to inference, with the reason attached: **the adjustment that
would rescue 2026-08-12 is precisely the one that would void the clear it was
measured under.**

Seven quiet days is unremarkable against a cleared run that scheduled 41 trades
in two years — one per eighteen days — and would remain so at fourteen. No
position was opened, so only selectivity was exercised, never skill.

## The defect you found was four slices wide

`human_note_path` read `…SLICE64.md` in slices **65, 66, 67 and 68**.

**Cause:** each slice's tool is derived from its predecessor by lower-case
substitution (`slice65`→`slice66`). The constant is `HUMAN_DATA_NOTE_SLICE64.md`
— upper case — so nothing ever matched it.

**Why it was worse than cosmetic:** `human_note_present` was computed from that
path, and every prior note survives in the tree, so the check returned `True`
**by verifying a file four slices old**. A presence check that could not fail.

**Blast radius, stated not minimised:** no scored claim was tainted. Note claims
are compared against values computed from the corpus, never parsed from the
note. The artefacts said true things about the right data while pointing at the
wrong path — for four slices.

## Two fixes, and the second one found three more

Correcting the string satisfies the ask; it would not stop the next one. So the
path is now **derived from the slice number**, and a new guard joins slice 66's
AST family: a `sliceNN_*` tool may not carry a string literal naming a slice
other than itself or its immediate predecessor unless annotated `prior-slice`.

The rule matches the defect exactly — a correct reference *advances every
slice*; anything older either advanced and stopped (the defect) or was always
fixed (a decision, which must be marked).

**On its first run it found three more instances nobody had noticed:**

| stale constant | frozen since | spelling |
|---|---|---|
| `items_complete_unchanged_from_slice_63` | slice 64 | `slice_63` |
| `unchanged_from_slice62` | slice 63 | `slice62` |
| `"no slice-63 forward artefact on this tree"` | slice 64 | `slice-63` |

Three different spellings, one cause — which is exactly why a substitution
pipeline cannot be made safe by adding more substitutions. It also caught a
stale narrative block claiming a pack regression that did not happen this slice;
it now reads `occurred_this_slice: false`.

Four deliberate cross-slice references survive and are annotated: the slice-55
test path, the slice-58 fingerprint pin, the slice-59 substitution lesson, and
the slice-61 parent named inside the pack-base claim. **An intentional
back-reference is a decision; an accidental one is a stale constant** — the
annotation is what separates them.

Slice 66's guard could not have caught any of this: a stale path is not an
equality against an integer literal. **A guard family grows one member per
lesson.**

## Evidence table

| claim | observed | file / command |
|---|---|---|
| window reached 7 | `after_t1_linear` 6 → 7 | `artifacts/slice69_data_freshness.json` |
| growth is real | prefix digests match slice-57 pins; append-only | `artifacts/slice69_data_freshness.json` |
| all 10 note claims true | scored from files | `test_every_note_claim_is_true` |
| 08-17 absent | clock-derived, cannot expire | `test_the_open_bar_is_absent` |
| **ceiling = 2** | `max(0, 7 − 5)`, declared §52b before the run | `artifacts/slice69_forward_shadow.json` |
| not 1, not 3 | both adjacent errors asserted against | `test_it_is_not_one_and_not_three` |
| 0 setups of 7 | every decision rate below `FUND_ABS` | `artifacts/slice69_data_freshness.json` |
| `FUND_ABS` unchanged | 0.0001; flag `false` | `test_fund_abs_is_unchanged` |
| join unchanged | 08-12 decision rate still 0.00006601 | `test_the_join_is_unchanged` |
| note path fixed | `…SLICE69.md`, derived not literal | `test_the_path_is_derived_not_a_literal` |
| defect was 4 slices wide | 65–68 all read `…SLICE64.md` | `test_the_defect_spanned_four_slices…` |
| no claim tainted | claims computed from corpus | `test_no_scored_claim_was_tainted` |
| new guard works | 3 control cases, incl. the exact defect | `test_the_guard_catches_the_defect_it_was_written_for` |
| no restore needed | artefact absent; 2nd clean pack | `test_no_restoration_was_needed_again` |
| meta-guards hold | AST sweeps clean | `test_the_meta_guards_are_all_enforced` |
| no edge measurement ran | artefacts carry no percentile/replicates | `test_no_edge_measurement_ran_this_slice` |
| gate refuses | 2 / 8, `allows_live` false | `artifacts/slice69_promotion_gate.json` |
| OOS untouched | `28b7dfe0…`; folds `ff5cc8a2…` | `test_the_oos_clear_was_not_re_scored` |
| registration discipline | HELD | `artifacts/slice69_registration_discipline.log` |
| paper session | green | `artifacts/slice69_paper_cert.log` |
| suite | 4,439 passed / 0 failed / 2 skipped | `artifacts/slice69_pytest.log` |

## Forbidden list — every item, answered

| forbidden | this slice |
|---|---|
| synthetic / invented / forward-filled bars | `bars_fabricated: 0` |
| including 2026-08-17 as closed | absent; asserted twice |
| relabelling bars ≤ `t1` as forward | strict at `t1` |
| trusting the note without verifying | 10 claims scored from files |
| re-scoring OOS / Stage-1 | artefact byte-identical |
| changing constants, schedule, caps, monitors, `FUND_ABS`, join | all asserted unchanged |
| raising `max_notional_usd` above 100 | 100.00 |
| arming live, `models/current`, training, Colab | live dark; no weights in tree |
| reopening frozen families | 11 unchanged; no altcoins |
| `closer_to_autonomous_profit_agent = true` | false |
| shadow mean R as Stage-1 evidence | `forward_mean_net_r: null` |
| completing human checklist items in code | 0 |
| clamping unfinished exits | false |
| claiming growth when `after_t1_linear ≠ 7` | it is 7 |
| **claiming ceiling = 1 at N = 7** | asserted `!= 1` |
| **claiming ceiling ≥ 3 at N = 7** | asserted `< 3` |
| parameter rescue because setups stay quiet | none; reason recorded |
| stale `human_note_path` | fixed, derived, and guarded |

## Two things carried forward

1. **Four manifest entries remain frozen-wrong.** ETH/SOL linear should read
   1461; ETH funding 4383; SOL funding 4458. Broadcast fixed; residue remains.
2. **The funding seam at `2026-08-09T16:00Z` is still empty**, eight slices on.

## What would move this forward

A funding rate at or above `1e-4` **standing at a bar's close**, then a barrier
resolving inside the window. The window is long enough now; the regime is not
obliging. The gate asks for 20 completed forward trades and 180 forward days;
the pilot has 0 and 7.

**A larger ceiling is a larger denominator. The numerator has not moved.**
