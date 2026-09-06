# SLICE 67 — VERDICT

```
SLICE67_VERDICT:                    PASS_WITH_DEFECTS
extension_present:                  YES
after_t1_linear_bars:               5
after_t1_dates:                     ['2026-08-10','2026-08-11','2026-08-12','2026-08-13','2026-08-14']
linear_last:                        2026-08-14T00:00:00+00:00
linear_rows:                        1466
after_t1_funding:                   16       funding_last: 2026-08-15T00:00:00+00:00
new_linear_bars_since_slice66:      1
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
        final     0 failed / 4,340 passed / 2 skipped
```

**Why `PASS_WITH_DEFECTS`.** Every PASS condition is met and the ceiling is
correctly stated as 0. But the pack regression is at five occurrences, the
manifest residue is still four false entries, and — new this slice — **a human
note made a claim that is not true**. None of it taints a data claim. A clean
PASS would hide all three.

## Growth verified, and half a prediction confirmed

```
after_t1_linear    4 -> 5     ['2026-08-10' .. '2026-08-14']
linear rows     1465 -> 1466  last 2026-08-14 (closed 2026-08-15T00:00Z; checked 02:55Z)
funding rows    4397 -> 4399  after_t1 = 16
2026-08-15      correctly ABSENT
prefix @1461 / @4383 match the slice-57 pins — history untouched, append-only
ETH and SOL byte-identical to slice 66
```

§49b, written when the window was four bars, predicted `|W| = 5 → 0` and
`|W| = 6 → 1` **before the data existed**. The window is five and the ceiling is
zero. `TestThePredictionHeld` checks the observation against slice 66's *frozen
artefact* — the prediction as recorded, not as remembered.

**This is the last slice at which zero follows from the window being shorter
than the horizon.**

## Six zeros, and what they still do not mean

```
slice 62   1 bar    ceiling 0    observed 0
slice 63   1 bar    ceiling 0    observed 0
slice 64   2 bars   ceiling 0    observed 0
slice 65   3 bars   ceiling 0    observed 0
slice 66   4 bars   ceiling 0    observed 0
slice 67   5 bars   ceiling 0    observed 0
```

**Every one ran at ceiling 0.** The rule has never been given an opportunity;
six quiet days neither prove nor disprove the clear.

A second condition is independent of the window and has not moved in five
slices: **the rule has not signalled.** No forward funding rate has reached
`FUND_ABS = 1e-4`, and the newest print — 6.29e-06 — is the smallest yet. Four
of the five bars have that as their sole explanation. At `|W| = 6` a trade will
require the funding regime to change, not merely the window to lengthen.

## Defect 1 — the first false claim a human note has made

The note ends with a line no previous note carried:

> Pack base: post-restore slice66 tree (not slice-61)

```
                        slice61      slice66 (mine)   slice67 pack
EDGE.md                 2af62040     3f763e81         2af62040
signals/…_btc_v1.py     2668e74b     43f2a913         2668e74b
tests/…eligibility.py   12da76a1     f3d87e58         12da76a1
§45–§49 present         no           yes              NO
```

Across slices 62–66 the notes made claims about **data** and every one verified.
This is the first about **process** and the first to fail. That is not a
coincidence: a human can check a bar count by looking at the file they just
wrote; they cannot check which parent their build script used by looking at
anything. **The note records an intention the pipeline did not carry out.**

Scored in the same dict as the data claims so it cannot be quarantined into a
footnote — while `claim_vs_files_discrepancy` stays `false`, because the mission
defines it narrowly as *growth claimed but not present*, and a reader scanning
for that must not be answered by a different question.

Fifth occurrence. Recorded without accusation.

## Defect 2 — the manifest broadcast repaired; the residue not

**You did what slice 66 asked**: BTC updated to 1466 / 4399, ETH and SOL not
re-broadcast — the first slice in four where their declared counts stood still.

```
                    s65 decl   s66 decl   s67 decl   disk
BTC_USDT_1D             1464       1465       1466   1466   ok
ETH_USDT_1D             1464       1465       1465   1461   frozen, still wrong
SOL_USDT_1D             1464       1465       1465   1461   frozen, still wrong
BTC_USDT_FUNDING        4394       4397       4399   4399   ok
ETH_USDT_FUNDING        4394       4397       4397   4383   frozen, still wrong
SOL_USDT_FUNDING        4394       4397       4397   4458   frozen, still wrong
```

ETH and SOL now carry slice 66's broadcast values, wrong when written and wrong
now. They were **moving-wrong**; they are **frozen-wrong**. A moving error
propagates; a frozen one merely persists.

**Slice 65's guard fired on this repair, by design.** It asserted all three
symbols declare one count and said in its docstring that if that stopped being
true the broadcast may have been repaired. Retired and replaced with two tests
asserting what is newly true — the broadcast is over, the residue is not. When
you correct those four numbers, the residue test fires; that will again be good
news.

## The meta-guard earned its keep on its first outing

**Slice 67 repaired zero live-absolute assertions in slice 66's tests — the
first slice in six that did not have to.** Slices 62–66 each spent budget
amending the previous slice's `appended_rows == N`; slice 66 stopped restating
the rule and made it unshippable with an AST sweep, and the recurrence stopped
immediately.

The cleanest available evidence for a principle this programme has circled since
§12c: **a guard that makes a mistake impossible beats a rule asking people not
to make it.**

## Evidence table

| claim | observed | file / command |
|---|---|---|
| window grew | `after_t1_linear` 4 → 5 | `artifacts/slice67_data_freshness.json` |
| growth is real, not the note | prefix digests match slice-57 pins; append-only | `artifacts/slice67_data_freshness.json` |
| 7 data claims true | each checked against files | `test_every_data_claim_the_note_made_is_true` |
| pack-base claim FALSE | 3 digests + zero §45–§49 sections | `test_the_pack_base_claim_is_false` |
| open bar absent | no bar dated ≥ today | `test_no_unclosed_bar_is_on_disk` |
| ceiling 0 at 5 bars | `max(0, 5 − 5)`, declared §50c before the run | `artifacts/slice67_forward_shadow.json` |
| §49b's prediction held | checked against slice 66's frozen artefact | `TestThePredictionHeld` |
| six zeros, all at ceiling 0 | read from each prior frozen artefact | `TestSixZerosAndWhatTheyMean` |
| rule stood aside on all 5 | no funding setup; 4 have that as sole reason | `artifacts/slice67_forward_shadow.json` |
| scoring not re-tuned | AST-equal per function vs slice 66 | `test_the_scoring_was_not_re_tuned_since_slice66` |
| broadcast stopped | BTC diverged from ETH/SOL | `test_the_broadcast_has_stopped` |
| residue remains | 4 entries frozen at slice-66 values | `test_the_residue_is_frozen_at_slice66s_broadcast_values` |
| meta-guard holds | AST sweep clean on every test file | `test_the_meta_guard_is_still_enforced` |
| no edge measurement ran | slice-67 artefacts carry no percentile/replicates | `test_no_edge_measurement_ran_this_slice` |
| gate refuses | 2 / 8, `allows_live` false | `artifacts/slice67_promotion_gate.json` |
| OOS untouched | `28b7dfe0…`; folds `ff5cc8a2…` | `test_the_oos_clear_was_not_re_scored` |
| registration discipline | HELD | `artifacts/slice67_registration_discipline.log` |
| paper session | green | `artifacts/slice67_paper_cert.log` |
| suite | 4,340 passed / 0 failed / 2 skipped | `artifacts/slice67_pytest.log` |

## Forbidden list — every item, answered

| forbidden | this slice |
|---|---|
| synthetic / invented / forward-filled bars | `bars_fabricated: 0` |
| relabelling bars ≤ `t1` as forward | strict at `t1`; pinned by tests |
| trusting the note without verifying | 8 claims scored; the false one found |
| re-scoring OOS / Stage-1 | artefact byte-identical; flag false |
| changing constants, schedule, caps, thresholds | fingerprint `662de011…`; AST-equal scoring |
| raising `max_notional_usd` above 100 | 100.00, asserted |
| arming live, `models/current`, training | live dark; `policy_mode` off |
| reopening any of the 11 frozen families | 11 unchanged; ETH/SOL not measured |
| `closer_to_autonomous_profit_agent = true` | false |
| shadow mean R as Stage-1 evidence | `forward_mean_net_r: null` |
| completing human checklist items in code | `human_items_completed_in_code: 0` |
| clamping unfinished exits | `exit_clamping_to_corpus_end: false` |
| claiming growth when `after_t1_linear` is 4 | it is 5, from disk |
| claiming observation when trades are 0 | `is_forward_observation: false` |
| requiring an open 2026-08-15 bar | absent, and its absence is asserted |
| running edge measurement or control batteries | none; asserted by artefact scan |
| **claiming ceiling ≥ 1 while `after_t1_linear` < 6** | ceiling is 0; the milestone is explicitly *not* claimed |

## Three things carried forward for the human

1. **The pack-base fix did not reach the pack builder.** The note says it did.
   Build slice 68 from `tradingbot_slice67.zip` and verify by checking that
   `EDGE.md` contains §45–§50 before shipping.
2. **Four manifest entries remain frozen-wrong.** ETH/SOL linear should read
   1461; ETH funding 4383; SOL funding 4458. The broadcast is fixed — this is
   the residue.
3. **The funding seam at `2026-08-09T16:00Z` is still empty**, six slices on.

## What would move this forward

**One more closed daily bar.** At `|W| = 6` the ceiling becomes 1 and a zero
would, for the first time, carry information. But the rule must also signal, and
no forward funding rate has come close to `1e-4` — so the milestone makes a
trade *possible*, not *likely*, and those must not be conflated when it arrives.

**The pilot has five post-`t1` days and zero forward observations.**
