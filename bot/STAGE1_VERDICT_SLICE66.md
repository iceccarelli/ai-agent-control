# SLICE 66 — VERDICT

```
SLICE66_VERDICT:                    PASS_WITH_DEFECTS
extension_present:                  YES
after_t1_linear_bars:               4
after_t1_dates:                     ['2026-08-10','2026-08-11','2026-08-12','2026-08-13']
linear_last:                        2026-08-13T00:00:00+00:00
linear_rows:                        1465
after_t1_funding:                   14       funding_last: 2026-08-14T08:00:00+00:00
new_linear_bars_since_slice65:      1
the_window_grew:                    YES

forward_n_trades:                   0
forward_observations_to_date:       0
is_forward_observation:             false
ceiling_max_possible_trades:        0
within_ceiling:                     YES

oos_evidence_re_scored:             NO
shadow_schedule_one_per_run:        YES
constants_fingerprint_match:        YES  662de0115880871352d5d623b1020eaa
caps_usd_100:                       YES
monitor_thresholds_unchanged:       YES
forward_monitor_status:             INSUFFICIENT_DATA
promotion_gate_allows_live:         NO   (2 of 8, unchanged)
live_authorized:                    false
policy_mode:                        off
models_current_present:             false
frozen_absent_count:                11
ProjectStatus.cleared_edge_signal:  funding_carry_fade_btc_v1
bars_fabricated:                    0
Closer to autonomous profit agent?: NO

suite:  baseline 12 failed / 4,032 passed / 2 skipped
        final     0 failed / 4,287 passed / 2 skipped
```

**Why `PASS_WITH_DEFECTS`.** Every PASS condition is met. But the tree shipped
onward still carries four false manifest assertions — third slice running — and
the pack regression is now at four. Both are inherited, both taint no claim,
both are pinned by tests. A clean PASS would hide a pattern the human needs.

## The growth is real and matches the claim exactly

```
after_t1_linear    3 -> 4     ['2026-08-10','2026-08-11','2026-08-12','2026-08-13']
linear rows     1464 -> 1465  last 2026-08-13 (closed 2026-08-14T00:00Z; checked 08:34Z)
funding rows    4394 -> 4397  after_t1 = 14
2026-08-14      correctly ABSENT
prefix @1461 / @4383 both match the slice-57 pins — history untouched, append-only
ETH and SOL byte-identical to slice 65
```

All seven note claims verified against files. The note's `cce454a6…` is again
the **compressed** digest — fourth slice running.

## This is the last structural zero, and the next two slices are predicted

```
    |W| = 4   ceiling 0     THIS SLICE
    |W| = 5   ceiling 0     the sixth bar a trade needs does not exist
    |W| = 6   ceiling 1     the first arithmetically possible forward trade
```

**Two more closed days take the ceiling off zero for the first time since `t1`
was locked.** Recorded before the data exists, derived from the frozen `HORIZON`
rather than from literals, and asserted by
`test_the_prediction_five_still_zero_six_becomes_one`. If `|W| = 6` arrives and
the ceiling is still zero, the defect is in this reasoning and the test says so.

A ceiling of one is **not** a forecast of a trade — it is the removal of the
reason there could not be one. The rule must still signal, and it has not: no
funding rate in the forward window has reached `FUND_ABS = 1e-4`. Three of the
four bars now have that as their *sole* explanation; the structural last-bar
exclusion covers only one.

## Five zeros, and the moment they start meaning something

```
slice 62   window 1 bar    ceiling 0    observed 0
slice 63   window 1 bar    ceiling 0    observed 0
slice 64   window 2 bars   ceiling 0    observed 0
slice 65   window 3 bars   ceiling 0    observed 0
slice 66   window 4 bars   ceiling 0    observed 0
```

Five zeros invite one misreading — a cleared rule failing forward. The ceiling
was zero in every one, established from each prior slice's **own frozen
artefact**. **Nothing in this sequence is evidence for or against the edge.**
From `|W| = 6` that stops being true and a zero would carry information.

## The recurring test defect, made unshippable

Five consecutive slices amended the previous slice's tests for pinning a **live
absolute** — `appended_rows == 1`, `== 2`, `== 3`, hard-coded dates. Slice 65
wrote the rule down and broke it three times in its own file. A rule that needs
restating every slice is not working.

`TestNoTestPinsALiveAbsolute` walks every test file's AST and fails any equality
between a live corpus count and a **positive** integer literal. AST, not text
search — §12c, seven times over.

**`== 0` is deliberately permitted**, and that is not a convenience: zero is the
only value that is not a snapshot of a growing quantity. It asserts growth has
*not* occurred — the claim "ETH and SOL were never extended" that carries the
manifest finding. When a `== 0` fails, the failure **is** the finding; when a
`== 4` fails, the test was merely stale. Five control cases (two offences, three
innocents) prove it discriminates; a third test proves the sweep reached the
slice files rather than returning an empty set.

## Defect 1 — the manifest broadcast, third occurrence

```
                    s64 decl   s65 decl   s66 decl   disk
ETH_USDT_1D             1463       1464       1465   1461
SOL_USDT_1D             1463       1464       1465   1461
ETH_USDT_FUNDING        4392       4394       4397   4383
SOL_USDT_FUNDING        4392       4394       4397   4458   UNDERSTATES
```

ETH's and SOL's files are byte-identical to slice 65's; declared counts moved
anyway. The shape checks slice 65 added caught it before any value comparison.
**SOL funding has now been declared wrong in three successive and different
ways while the file has held 4458 throughout** — now pinned as a sequence, with
assertions that all three differ and none equals disk.

Still not edited: a record rewritten to agree with disk certifies nothing.

## Defect 2 — fourth consecutive pack from the slice-61 tree

```
                        slice61     62         63         64         65         66 pack
EDGE.md                 2af62040    2ee9acf4   c4deed69   675e021d   24ddd77a   2af62040
signals/…_btc_v1.py     2668e74b    43f2a913   43f2a913   43f2a913   43f2a913   2668e74b
tests/…eligibility.py   12da76a1    0d314207   0d314207   046f0d4c   aae96ea1   12da76a1
```

Everything from slices 62–65 absent; baseline identical to 64 and 65. Restored
byte-identically; dated records restored not regenerated, checkable because each
carries a `git_commit` absent from this history.

## Evidence table

| claim | observed | file / command |
|---|---|---|
| window grew | `after_t1_linear` 3 → 4 | `artifacts/slice66_data_freshness.json` |
| growth is real, not the note | prefix digests match slice-57 pins; append-only | `artifacts/slice66_data_freshness.json` |
| all 7 note claims true | checked against files | `test_every_note_claim_was_checked_against_the_files` |
| open bar absent | no bar dated ≥ today | `test_no_unclosed_bar_is_on_disk` |
| ceiling still 0 | `max(0, 4 − 5)`, declared §49b before the run | `artifacts/slice66_forward_shadow.json` |
| prediction 5→0, 6→1 | derived from frozen HORIZON | `test_the_prediction_five_still_zero_six_becomes_one` |
| five zeros, all at ceiling 0 | read from each prior frozen artefact | `TestFiveZerosAndWhatTheyMean` |
| rule stood aside on all 4 | no funding setup; 3 have that as sole reason | `artifacts/slice66_forward_shadow.json` |
| scoring not re-tuned | AST-equal per function vs slice 65 | `test_the_scoring_was_not_re_tuned_since_slice65` |
| no live absolutes remain | AST sweep of every test file | `TestNoTestPinsALiveAbsolute` |
| no edge measurement ran | slice-66 artefacts carry no percentile/replicates | `test_no_edge_measurement_ran_this_slice` |
| manifest defect, 3rd time | 4 entries false; SOL wrong 3 different ways | `TestTheManifestsDescribeTheFilesBesideThem` |
| dated records not regenerated | commits absent from this history | `test_the_dated_records_were_not_regenerated_on_this_tree` |
| gate refuses | 2 / 8, `allows_live` false | `artifacts/slice66_promotion_gate.json` |
| OOS untouched | `28b7dfe0…`; folds `ff5cc8a2…` | `test_the_oos_clear_was_not_re_scored` |
| registration discipline | HELD | `artifacts/slice66_registration_discipline.log` |
| paper session | green | `artifacts/slice66_paper_cert.log` |
| suite | 4,287 passed / 0 failed / 2 skipped | `artifacts/slice66_pytest.log` |

## Forbidden list — every item, answered

| forbidden | this slice |
|---|---|
| synthetic / invented / forward-filled bars | `bars_fabricated: 0`; no corpus written by any tool |
| relabelling bars ≤ `t1` as forward | `in_forward_window` strict at `t1`; pinned by tests |
| trusting the note without verifying | 7 claims re-derived from files |
| re-scoring OOS / Stage-1 | artefact byte-identical; flag false |
| changing constants, schedule, caps, thresholds | fingerprint `662de011…`; AST-equal scoring |
| raising `max_notional_usd` above 100 | 100.00, asserted |
| arming live, `models/current`, training | live dark; `policy_mode` off; no model |
| reopening any of the 11 frozen families | 11 unchanged; ETH/SOL not measured |
| `closer_to_autonomous_profit_agent = true` | false |
| shadow mean R as Stage-1 evidence | `forward_mean_net_r: null` |
| completing human checklist items in code | `human_items_completed_in_code: 0` |
| clamping unfinished exits | `exit_clamping_to_corpus_end: false` |
| claiming growth when `after_t1_linear` is 3 | it is 4, from disk; delta 3 → 4 recorded |
| claiming observation when trades are 0 | `is_forward_observation: false` |
| requiring an open 2026-08-14 bar | absent, and its absence is asserted |
| running edge measurement or control batteries | none; asserted by artefact scan |

## Three things carried forward for the human

1. **Build slice 67 from `tradingbot_slice66.zip`.** Four consecutive packs
   have been the slice-61 tree; four slices have paid an hour each for it.
2. **Repair the MANIFESTs, or stop regenerating them from BTC.** SOL funding
   should read 4458. When repaired, the enumeration goes red — that is the
   signal to retire it.
3. **The funding seam at `2026-08-09T16:00Z` is still empty**, five slices on.

## What would move this forward

**Two more closed daily bars.** At `|W| = 6` the ceiling becomes 1 and, for the
first time, a zero would mean something. The rule must then also signal, which
it has not yet done — no forward funding rate has reached `1e-4`.

**The pilot has four post-`t1` days and zero forward observations.**
