# SLICE 68 — VERDICT

```
SLICE68_VERDICT:                    PASS
extension_present:                  YES
after_t1_linear_bars:               6
after_t1_dates:                     ['2026-08-10','2026-08-11','2026-08-12',
                                     '2026-08-13','2026-08-14','2026-08-15']
linear_last:                        2026-08-15T00:00:00+00:00
linear_rows:                        1467
new_linear_bars_since_prior:        1        (slice 67 → 68)
the_window_grew:                    YES

forward_n_trades:                   0
forward_observations_to_date:       0
is_forward_observation:             false
ceiling_max_possible_trades:        1        <-- FIRST structural non-zero
within_ceiling:                     YES
funding_setups_in_window:           0 of 6

oos_evidence_re_scored:             NO
shadow_schedule_one_per_run:        YES
constants_fingerprint_match:        YES  662de0115880871352d5d623b1020eaa
caps_usd_100:                       YES
monitor_thresholds_unchanged:       YES
forward_monitor_status:             INSUFFICIENT_DATA
promotion_gate_allows_live:         NO   (2 of 8, unchanged)
live_authorized:                    false
models_current_present:             false
frozen_absent_count:                11
ProjectStatus.cleared_edge_signal:  funding_carry_fade_btc_v1
bars_fabricated:                    0
Closer to autonomous profit agent?: NO

suite:  baseline 1 failed / 4,339 passed / 2 skipped
        final    0 failed / 4,389 passed / 2 skipped
```

**Why `PASS` and not `PASS_WITH_DEFECTS`.** The pack arrived intact — it *is*
the slice-67 deliverable — so the defect that forced the last four verdicts down
is gone. All nine note claims verify. The one residual issue, four stale
manifest entries for ETH/SOL, is a known, pinned, non-propagating residue that
touches no claim in this slice and is already enumerated by tests. Nothing here
taints data truth or process truth.

## The ceiling opened, exactly where it was predicted

§49b, written from a **four**-bar window, declared `|W| = 5 → 0` and
`|W| = 6 → 1`. §50c confirmed the first half. This is the second.

```
    closed forward bars                              6
    HORIZON                                          5
    max_possible_forward_closed_trades               1      = max(0, 6 − 5)
    observed                                         0
    within_ceiling                                   YES
```

`TestThePredictionCompleted` checks this against the **frozen artefacts that
recorded the prediction**, not against a fresh computation — a check on the
claim rather than a restatement of the formula.

A ceiling of one removes an obstacle. Three conditions gate a completed forward
trade; only the first changed:

| condition | status |
|---|---|
| (1) enough closed bars | **now true, first time** |
| (2) a funding setup fires, `\|f\| ≥ FUND_ABS` | not true — **0 of 6** |
| (3) `one_entry_per_contiguous_run` schedule | unchanged, frozen |

The ceiling also counts **decision bars, not outcomes**: exactly one forward bar
could yield a trade closing inside the window, and whether it closes depends on
the barrier resolving in the bars that remain. A true upper bound, not a
forecast.

## One print landed exactly on the threshold

```
2026-08-12T00:00:00Z    0.00010000     ==  FUND_ABS, exactly
2026-08-12T08:00:00Z    0.00008568
2026-08-12T16:00:00Z    0.00006601     <-- the rate AT THE DECISION
```

`funding_setups` compares `f >= fund_abs` — **inclusive**. A test now asserts
that from the code's *behaviour*, not its docstring: it finds every bar in the
corpus whose rate equals `FUND_ABS` and requires each to be a setup. So a
decision taken against that print would have been a SHORT.

It wasn't, because `funding_at_decision` reads
`funding.at_or_before(close_time_ms(bar))` — the rate standing at the bar's
**close** — and two later prints had superseded it.

**Not a missed trade and not a defect.** The join reads only backwards in time,
which is what makes it honest. Recorded because it is the closest the window has
come in seven slices, and because *"a qualifying rate existed that day"* and
*"the qualifying rate stood at the decision"* are different claims a later
reader could collapse into a phantom trade.

**No parameter moved.** `FUND_ABS` stays 0.0001, horizon 5, join close-time. A
threshold adjusted after seeing which prints missed it is a threshold fitted to
the data — and **a narrow miss makes the temptation larger, not the change more
defensible**. The constants and fingerprint are asserted in the same test file,
immediately after the near-miss, so the two are read together.

## What this zero means — stated narrowly

```
slice 62   1 bar    ceiling 0    setups 0    trades 0
slice 63   1 bar    ceiling 0    setups 0    trades 0
slice 64   2 bars   ceiling 0    setups 0    trades 0
slice 65   3 bars   ceiling 0    setups 0    trades 0
slice 66   4 bars   ceiling 0    setups 0    trades 0
slice 67   5 bars   ceiling 0    setups 0    trades 0
slice 68   6 bars   ceiling 1    setups 0    trades 0   <-- first informative row
```

**What it says:** the funding regime over 2026-08-10…15 produced no qualifying
rate at any decision. A fact about the market under a frozen rule — the first
forward fact of any kind this pilot has produced.

**What it does not say:** anything about whether the rule makes money. No
position was opened, so only its **selectivity** was exercised, never its skill.
The cleared run scheduled 41 trades over two years — about one per eighteen days
— so six quiet days is unremarkable under the null and the alternative alike.
That base rate is written into the artefact so it cannot be argued from memory.

**Honest headline: "ceiling 1, zero setups, zero fills."** Neither "the rule
failed to trade" nor "the rule is being careful."

## The pack regression ended

`EDGE.md` at `2492e7f2bca7` — identical to the slice-67 deliverable. §45–§50 all
present. Every tool and test from slices 62–67 in place. **No restoration
artefact exists this slice because there was nothing to restore**, the first time
since slice 62, and a test asserts its absence.

Slice 67's note claimed a post-restore base and the digests disagreed (§50a).
The claim is re-scored here on the same terms rather than dropped — **a claim
that failed once is exactly the one worth checking again** — and this time it is
true. All nine note claims verify.

## Evidence table

| claim | observed | file / command |
|---|---|---|
| window reached 6 | `after_t1_linear` 5 → 6 | `artifacts/slice68_data_freshness.json` |
| growth is real | prefix digests match slice-57 pins; append-only | `artifacts/slice68_data_freshness.json` |
| all 9 note claims true | incl. the pack-base claim that failed in 67 | `test_every_note_claim_is_true_this_time` |
| open bar absent | no bar dated ≥ today | `test_no_unclosed_bar_is_on_disk` |
| **ceiling = 1** | `max(0, 6 − 5)`, declared §51b before the run | `artifacts/slice68_forward_shadow.json` |
| prediction completed | checked vs slices 66 & 67 frozen artefacts | `TestThePredictionCompleted` |
| ceiling ≠ 0 and < 2 | asserted both ways | `TestThePredictionCompleted` |
| 0 setups of 6 | every decision rate below `FUND_ABS` | `artifacts/slice68_data_freshness.json` |
| threshold touched once | `2026-08-12T00:00Z` == `FUND_ABS` exactly | `test_exactly_one_forward_print_reached_the_threshold` |
| comparison is inclusive | derived from behaviour, not docstring | `test_the_comparison_is_inclusive` |
| join reads bar close | decision rate is the 16:00 print | `test_the_join_reads_the_close_not_the_open` |
| no parameter moved | `FUND_ABS` 0.0001; fingerprint unchanged | `test_no_parameter_moved_because_of_the_near_miss` |
| pack intact | `EDGE.md` == slice-67; §45–§51 present | `TestThePackArrivedIntact` |
| no restore needed | artefact absent | `test_no_restoration_was_needed` |
| meta-guard holds | AST sweep clean on every test file | `test_the_meta_guard_is_still_enforced` |
| no edge measurement ran | artefacts carry no percentile/replicates | `test_no_edge_measurement_ran_this_slice` |
| gate refuses | 2 / 8, `allows_live` false | `artifacts/slice68_promotion_gate.json` |
| OOS untouched | `28b7dfe0…`; folds `ff5cc8a2…` | `test_the_oos_clear_was_not_re_scored` |
| registration discipline | HELD | `artifacts/slice68_registration_discipline.log` |
| paper session | green | `artifacts/slice68_paper_cert.log` |
| suite | 4,389 passed / 0 failed / 2 skipped | `artifacts/slice68_pytest.log` |

## Forbidden list — every item, answered

| forbidden | this slice |
|---|---|
| synthetic / invented / forward-filled bars | `bars_fabricated: 0` |
| relabelling bars ≤ `t1` as forward | strict at `t1`; pinned by tests |
| trusting the note without verifying | 9 claims scored from files |
| re-scoring OOS / Stage-1 | artefact byte-identical; flag false |
| changing constants, schedule, caps, thresholds | fingerprint `662de011…`; `FUND_ABS` 0.0001 |
| raising `max_notional_usd` above 100 | 100.00, asserted |
| arming live, `models/current`, training, Colab | live dark; `policy_mode` off; no weights in tree |
| reopening any of the 11 frozen families | 11 unchanged; no altcoin expansion |
| `closer_to_autonomous_profit_agent = true` | false |
| shadow mean R as Stage-1 evidence | `forward_mean_net_r: null` |
| completing human checklist items in code | `human_items_completed_in_code: 0` |
| clamping unfinished exits | `exit_clamping_to_corpus_end: false` |
| claiming growth when `after_t1_linear` < 6 | it is 6, from disk |
| **claiming ceiling = 0 at N = 6** | ceiling is 1; asserted ≠ 0 |
| **claiming ceiling ≥ 2 at N = 6** | asserted < 2 |
| parameter rescue because setups are quiet | none; asserted after the near-miss |
| "almost positive" / multi-symbol rescue | absent |

## Two things carried forward for the human

1. **Keep building from the previous deliverable.** It worked this slice — the
   verification is `EDGE.md` containing §45–§51 before you ship.
2. **Four manifest entries remain frozen-wrong.** ETH/SOL linear should read
   1461; ETH funding 4383; SOL funding 4458. The broadcast is fixed; this is
   residue. When corrected, the residue test fires — that is the signal to
   retire it.

## What would move this forward

**A funding rate at or above `1e-4` standing at a bar's close**, followed by a
barrier resolving inside the window. The window is now long enough; the regime
is not obliging. The gate asks for 20 completed forward trades and 180 forward
days; the pilot has 0 and 6.

**Nothing about this slice is closer to autonomy. One structurally possible
trade is a denominator, not a numerator.**
