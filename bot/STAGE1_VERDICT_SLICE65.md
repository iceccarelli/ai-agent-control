# SLICE 65 — VERDICT

```
SLICE65_VERDICT:                    PASS_WITH_DEFECTS
extension_present:                  YES
after_t1_linear_bars:               3
after_t1_dates:                     ['2026-08-10', '2026-08-11', '2026-08-12']
linear_last:                        2026-08-12T00:00:00+00:00
linear_rows:                        1464
after_t1_funding:                   11       funding_last: 2026-08-13T08:00:00+00:00
new_linear_bars_since_slice64:      1
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
        final     0 failed / 4,237 passed / 2 skipped
```

**Why `PASS_WITH_DEFECTS`.** Every PASS condition is met — growth verified from
files, forward segment honestly computed, ceiling declared and respected, gate
refusing, clear intact, pack frozen, suite green. But the tree shipped onward
still carries four false manifest assertions, and this is the second slice
running for that defect and the third for the pack regression. Both are
inherited, both taint no claim, and both are pinned by tests. Calling it a clean
PASS would hide a pattern the human needs to see.

## The growth is real and matches the claim exactly

```
after_t1_linear    2 -> 3      ['2026-08-10', '2026-08-11', '2026-08-12']
linear rows     1463 -> 1464   last 2026-08-12 (closed 2026-08-13T00:00Z; checked 10:14Z)
funding rows    4392 -> 4394   after_t1 = 11
2026-08-13      correctly ABSENT
prefix @1461 / @4383 both match the slice-57 pins — history untouched, append-only
ETH and SOL byte-identical to slice 64
```

The note's `39dcd468…` is again the **compressed** digest and again matches —
third slice running for a distinction §46c recorded two slices ago.

## Zero is now a sequence, and a sequence has to be read

```
slice 62   window 1 bar    ceiling 0    observed 0
slice 63   window 1 bar    ceiling 0    observed 0
slice 64   window 2 bars   ceiling 0    observed 0
slice 65   window 3 bars   ceiling 0    observed 0
```

Read carelessly this looks like a rule failing four times. **The ceiling has
been zero in every one**, so the rule has never yet been given an opportunity to
produce a trade. `TestZeroIsNotThreeFailedAttempts` establishes that from each
prior slice's own frozen artefact rather than from memory — had any of them run
with a positive ceiling and still returned nothing, the sequence would mean the
opposite.

The earliest slice at which a forward trade is arithmetically possible is the
one with **six** closed forward bars: three more days than exist today.

One detail sharpens with the third bar. The structural last-bar exclusion covers
exactly one bar, so for `2026-08-10` and `2026-08-11` the **funding threshold is
now the sole explanation** — no rate in the forward window has reached
`FUND_ABS = 1e-4`.

## Defect 1 — the manifest broadcast recurred, and moved

Slice 64 enumerated four false entries and said the enumeration would go red
*"the moment any of them changes — including when a human FIXES them."* It went
red. The signal reads **re-broadcast**:

```
                        slice64 declared   slice65 declared   disk
ETH_USDT_1D                       1463               1464     1461
SOL_USDT_1D                       1463               1464     1461
ETH_USDT_FUNDING                  4392               4394     4383
SOL_USDT_FUNDING                  4392               4394     4458   UNDERSTATES by 64
```

ETH's and SOL's files are byte-identical to slice 64's, and their declared
counts moved anyway, tracking BTC's. That settles a question slice 64 could only
raise: the manifest is **regenerated from BTC each slice**, so its ETH and SOL
entries carry no information at all. SOL funding — 4458 prints, never changed —
has now been declared wrong in two successive and *different* ways.

Two checks were added that assert the **shape** rather than the values (all
three symbols declaring one count while the files hold three; declared counts
moving while files do not), so the next regeneration is caught before anyone
compares numbers. Still not edited: a record rewritten to agree with disk
certifies nothing, and this programme cannot attest a provenance it did not
observe.

## Defect 2 — third consecutive pack from the slice-61 tree

```
                        slice61      slice62     slice63     slice64     slice65 pack
EDGE.md                 2af62040     2ee9acf4    c4deed69    675e021d    2af62040
signals/…_btc_v1.py     2668e74b     43f2a913    43f2a913    43f2a913    2668e74b
tests/…eligibility.py   12da76a1     0d314207    0d314207    046f0d4c    12da76a1
```

Everything from slices 62, 63 **and** 64 absent; baseline identical to slice
64's down to the failing test names. Three occurrences is a fixed step in the
pack-building process, not an accident. Restored byte-identically; dated records
restored rather than regenerated, checkable because each carries a `git_commit`
absent from this repository's history.

## The lesson, applied to this programme's own work

Four tests written in slice 64 pinned **live absolutes** — `appended_rows == 2`,
a hard-coded `2026-08-12`, slice-64 row counts against today's disk — and
expired when the third bar arrived, in the same file whose EDGE section had just
named that mistake. Recorded, not quietly repaired.

The clearest repair: `test_no_bar_for_the_current_utc_day_is_present` computes
today from the clock instead of naming a date, so it states the real rule — *an
unclosed bar is never on disk* — and cannot expire.

A fifth amendment was the same shape one level up: slice 64's restoration test
re-verified the restored *apparatus* against live files, exactly as slice 63's
had. Recurrence at two consecutive levels means a repair should have been a
rule, so it is now written as one:

> **Each slice's restoration manifest owns the live apparatus; every earlier one
> owns only its frozen records.**

## Evidence table

| claim | observed | file / command |
|---|---|---|
| window grew | `after_t1_linear` 2 → 3 | `artifacts/slice65_data_freshness.json` |
| growth is real, not the note | prefix digests match slice-57 pins; append-only | `artifacts/slice65_data_freshness.json` |
| every note claim true | 7 of 7 checked against files | `test_the_growth_claim_matches_the_files` |
| open bar absent | no bar dated ≥ today | `test_no_unclosed_bar_is_on_disk` |
| ceiling still 0 | `max(0, 3 − 5)`, declared §48b before the run | `artifacts/slice65_forward_shadow.json` |
| zero ≠ failed attempts | all four prior slices ran at ceiling 0 | `TestZeroIsNotThreeFailedAttempts` |
| rule stood aside on all 3 | no funding setup; only 1 bar is last-bar | `artifacts/slice65_forward_shadow.json` |
| scoring not re-tuned | AST-equal per function vs slice 64 | `test_the_scoring_was_not_re_tuned_since_slice64` |
| manifest defect recurred | 4 entries false, values moved, files did not | `TestTheManifestsDescribeTheFilesBesideThem` |
| defect is a broadcast | all 3 symbols declare one count | `test_the_defect_is_a_broadcast_and_not_four_coincidences` |
| BTC entries accurate | both agree with their files | `test_the_measured_products_entries_are_accurate` |
| dated records not regenerated | their commits absent from this history | `test_the_dated_records_were_not_regenerated_on_this_tree` |
| gate refuses | 2 / 8, `allows_live` false | `artifacts/slice65_promotion_gate.json` |
| OOS untouched | `28b7dfe0…`; folds `ff5cc8a2…` | `test_the_oos_clear_was_not_re_scored` |
| registration discipline | HELD | `artifacts/slice65_registration_discipline.log` |
| paper session | green | `artifacts/slice65_paper_cert.log` |
| suite | 4,237 passed / 0 failed / 2 skipped | `artifacts/slice65_pytest.log` |

## Forbidden list — every item, answered

| forbidden | this slice |
|---|---|
| synthetic / invented / forward-filled bars | `bars_fabricated: 0`; no corpus written by any tool |
| relabelling bars ≤ `t1` as forward | `in_forward_window` strict at `t1`; pinned by tests |
| trusting the note without verifying | 7 claims re-derived from files; digest identified as compressed |
| re-scoring OOS / Stage-1 | artefact byte-identical; `cleared_edge_re_scored_this_slice: false` |
| changing constants, schedule, caps, thresholds | fingerprint `662de011…`; AST-equal scoring |
| raising `max_notional_usd` above 100 | 100.00, asserted |
| arming live, `models/current`, training | live dark; `policy_mode` off; no model |
| reopening any of the 11 frozen families | 11 unchanged; ETH/SOL not measured |
| `closer_to_autonomous_profit_agent = true` | false |
| shadow mean R as Stage-1 evidence | `forward_mean_net_r: null`; both flags false |
| completing human checklist items in code | `human_items_completed_in_code: 0` |
| clamping unfinished exits | `exit_clamping_to_corpus_end: false` |
| slicing a tiny post-`t1` window | `bar_array_sliced: false`; flags zeroed |
| claiming growth when `after_t1_linear` is 2 | it is 3, from disk; delta 2 → 3 recorded |
| claiming observation when trades are 0 | `is_forward_observation: false` |
| requiring an open 2026-08-13 bar | absent, and its absence is asserted |
| rebranding historical M4 WARN | forward M4 `INSUFFICIENT_DATA`; WARN stands unerased |

## Three things carried forward for the human

1. **Build slice 66 from `tradingbot_slice65.zip`.** Three consecutive packs
   have been the slice-61 tree. This is a one-line change where the zip is
   assembled, and it costs an hour of every slice until it is made.
2. **Repair the MANIFESTs, or stop regenerating them from BTC.** The ETH and SOL
   entries are a restatement of BTC's numbers; SOL funding should read 4458.
   When repaired, the enumeration goes red — that is the signal to retire it.
3. **The funding seam at `2026-08-09T16:00Z` is still empty**, four slices on.

## What would move this forward

**Three more closed daily bars** — six is the arithmetic minimum. And then the
rule must actually signal, which on all three forward days it did not, for want
of a funding rate above `1e-4`. The gate wants 20 closed forward trades and 180
days; neither moved, and neither should have.

**The pilot has three post-`t1` days and zero forward observations.**
