# SLICE 63 — VERDICT

```
SLICE63_VERDICT:                    PASS
extension_present:                  YES
after_t1_linear_bars:               1
after_t1_dates:                     ['2026-08-10']
linear_last:                        2026-08-10T00:00:00+00:00
after_t1_funding:                   6
funding_last:                       2026-08-11T16:00:00+00:00

new_linear_bars_since_slice62:      0        <-- the window did NOT grow
new_funding_prints_since_slice62:   1

forward_n_trades:                   0
forward_observations_to_date:       0
is_forward_observation:             false
ceiling_max_possible_trades:        0
within_ceiling:                     YES

oos_evidence_re_scored:             NO
shadow_schedule_one_per_run:        YES
constants_fingerprint_match:        YES   662de0115880871352d5d623b1020eaa
caps_usd_100:                       YES   (1 position / 1 entry per day)
monitor_thresholds_unchanged:       YES
forward_monitor_status:             INSUFFICIENT_DATA
promotion_gate_allows_live:         NO    (2 of 8, unchanged)
live_authorized:                    false
policy_mode:                        off
models_current_present:             false
frozen_absent_count:                11
ProjectStatus.cleared_edge_signal:  funding_carry_fade_btc_v1
bars_fabricated:                    0
Closer to autonomous profit agent?: NO

suite:  baseline 8 failed / 4,036 passed / 2 skipped
        final    0 failed / 4,141 passed / 2 skipped
```

## Two findings, and neither of them is a number

### 1. The pack is the slice-61 tree

Everything slice 62 produced is absent from the delivered pack: `corpus_prefix`,
the three `slice62_*` tools, `test_slice62_forward_extension.py`, all seven
`artifacts/slice62_*`, `STAGE1_VERDICT_SLICE62.md`, the forward-window helpers,
the repaired tests, and EDGE.md §45.

Not claimed from a file listing. Two independent lines of evidence:

```
EDGE.md                             slice61 & 63  2af62040...   slice62  2ee9acf4...
signals/funding_carry_fade_btc_v1.py slice61 & 63  2668e74b...   slice62  43f2a913...
tests/test_slice55_data_eligibility.py slice61&63  12da76a1...   slice62  0d314207...

baseline suite   slice 62   8 failed, 4036 passed, 2 skipped
                 slice 63   8 failed, 4036 passed, 2 skipped     THE SAME EIGHT
```

Recorded without accusation — a tree can be assembled from the wrong parent.
What matters is that the work was not in the pack, so this slice brought it
back, in three deliberately different ways:

| what | how | why |
|---|---|---|
| apparatus (prefix invariant, forward helpers) | restored **byte-identically** | a re-implementation that happened to agree is a second opinion presented as the original |
| dated records (`slice62_*.json`, verdict) | restored, **never regenerated** | re-running them today produces files that *look* like slice-62 records and are not |
| anything numbered 63 | written fresh | — |

**The "not regenerated" claim is checkable, not promised.** Each restored
artefact carries the `git_commit` of the tree it was written on, and a test
requires that commit to be **absent** from this repository's history. Anything
regenerated here would carry a commit that is present. Every byte is pinned in
`artifacts/slice63_restored_from_slice62.json`.

**The restored invariant passed on a corpus it had never seen, unmodified, first
run** — 238 passed across the four affected files. An invariant quietly fitted
to slice 62's numbers would have needed a nudge. That is a stronger statement
about §45c than slice 62 could make about itself.

### 2. The window did not grow

```
compressed    slice 62  227e5f04...    slice 63  db05f3f8...    DIFFERENT
uncompressed  slice 62  32ca5971...    slice 63  32ca5971...    IDENTICAL
```

The human note quotes `db05f3f8…`, which read alone says the linear corpus
changed. **It did not.** Both figures digest the *compressed* file, whose gzip
header varies between runs; the repository pins the *uncompressed* stream. The
linear corpus is byte-identical to slice 62's — 1462 rows, last bar 2026-08-10.

What arrived is **one funding print** (`2026-08-11T16:00Z`). The decision clock
is the daily bar, and slice 61's tool already wrote the rule down: *a funding
print without a bar to decide is not an observation.*

`t1` does not move, so `extension_present` is permanently true once one bar
lands and re-reporting it reads like progress. Both artefacts now carry the
**delta** beside it, computed against the previous slice's own artefact:
`new_linear_bars_since_slice62 = 0`, `window_grew = false`.

So slice 63 measured the window slice 62 measured. The zero below is not "zero
again" as though something were attempted and failed — **there was nothing new
to attempt**, and the artefacts say so in those terms.

## The forward segment

```
closed forward bars                          1     (2026-08-10, closed 2026-08-11T00:00Z)
closed forward bars needed for one trade     6     (HORIZON 5 + next_open entry)
max_possible_forward_closed_trades           0     declared in EDGE.md §46d before the run
observed_forward_closed_trades               0
within_ceiling                               TRUE
is_forward_observation                       FALSE
```

Independently, **the rule stood aside**: no funding setup on that bar (prints
7.5e-05, 7.9e-05, 5.1e-05, all inside `FUND_ABS = 1e-4`) and it is the last bar
of the corpus, which `directed_signal_bars` excludes on its own. Both computed,
not asserted.

**The scoring was not re-tuned.** `tools/slice63_forward_shadow.py` is compared
to slice 62's as *syntax trees*, per function: every function but `build`
identical outright; `build` split at its `return`, with everything before it —
the whole of the scoring — required identical, and every key slice 62 emitted
required to map to the same expression. Additions allowed; changes and removals
not. The guard was probed: perturbing the ceiling arithmetic by `+1` turns it
red; restoring the line turns it green.

## One test amended, and the amendment declared

`test_the_new_forward_helpers_are_purely_additive` excluded callers named
`slice62_*` — a **slice number standing in for a role** — so slice 63's own
forward-shadow tool failed it the moment it existed. Adding `slice63_*` would
have repeated the mistake. The rule now names the role (the defining module,
plus any `tools/slice<N>_forward_shadow.py`) and asserts six named scoring
modules absent so it cannot pass by sweeping nothing. **Strictly stronger**: the
old form would have admitted a hypothetical `slice62_edge_measurement.py`.

Both digests and the reason are recorded in the restoration manifest, and a test
refuses any digest change the manifest does not name. **A test is a living
document; a dated artefact is not.** No dated record was touched.

## Evidence table

| claim | observed | file / command |
|---|---|---|
| pack is the slice-61 tree | 3 digests + identical 8-failure baseline | `artifacts/slice63_restored_from_slice62.json` |
| dated records not regenerated | their `git_commit`s absent from this history | `test_the_dated_records_were_not_regenerated_on_this_tree` |
| history untouched | both prefix digests match the slice-57 pins | `artifacts/slice63_data_freshness.json` |
| append-only | all 6 corpus files | `artifacts/slice63_data_freshness.json` |
| linear identical to slice 62 | uncompressed `32ca5971…` | `artifacts/slice63_data_freshness.json` |
| note quotes compressed digest | matches the gz file, not the pin | `artifacts/slice63_data_freshness.json` |
| window did not grow | `new_linear_bars_since_slice62 = 0` | `artifacts/slice63_data_freshness.json` |
| forward trades | 0, equal to the declared ceiling | `artifacts/slice63_forward_shadow.json` |
| rule stood aside | no funding setup; last bar of corpus | `artifacts/slice63_forward_shadow.json` |
| no clamping, no slicing, no invention | all false / 0 | `artifacts/slice63_forward_shadow.json` |
| scoring not re-tuned | AST-equal per function | `test_the_scoring_logic_was_not_re_tuned_between_slices` |
| gate refuses | 2 / 8, `allows_live` false | `artifacts/slice63_promotion_gate.json` |
| folds unmodified | `ff5cc8a2…` | `artifacts/slice63_data_freshness.json` |
| OOS artefact untouched | `28b7dfe0…` | `test_the_oos_clear_was_not_re_scored` |
| registration discipline | HELD, three batteries | `artifacts/slice63_registration_discipline.log` |
| paper session | green | `artifacts/slice63_paper_cert.log` |
| suite | 4,141 passed / 0 failed / 2 skipped | `artifacts/slice63_pytest.log` |

## Forbidden list — every item, answered

| forbidden | this slice |
|---|---|
| synthetic / invented / forward-filled bars labelled real | `bars_fabricated: 0`; no corpus written by any tool |
| relabelling bars ≤ `t1` as forward | `in_forward_window` strict at `t1`; two tests pin it |
| trusting the human note without verifying | every claim re-derived from file hashes and dates; the note's own digest identified as the compressed one |
| re-scoring OOS / Stage-1 | `cleared_edge_re_scored_this_slice: false`; artefact byte-identical |
| changing constants, schedule, caps, thresholds | fingerprint `662de011…`; caps 1/1/100.00; thresholds unmoved; AST-equal scoring |
| raising `max_notional_usd` above 100 | 100.00, asserted |
| arming live, `models/current`, training | live dark; `policy_mode` off; no model |
| reopening any of the 11 frozen families | 11, unchanged; ETH/SOL not measured |
| `closer_to_autonomous_profit_agent = true` | false |
| shadow mean R as Stage-1 / registration evidence | `forward_mean_net_r: null`; both flags false |
| completing human checklist items in code | `human_items_completed_in_code: 0`; no signature |
| clamping unfinished exits | `exit_clamping_to_corpus_end: false` |
| slicing a tiny post-`t1` window | `bar_array_sliced: false`; flags zeroed |
| rebranding historical M4 WARN as forward | forward M4 reads `INSUFFICIENT_DATA`; the WARN stands unerased in `slice59_forward_shadow.json` |

## Two things carried forward for the human

1. **The slice-62 deliverable did not reach the slice-63 pack.** Building slice
   64 from `tradingbot_slice63.zip` (sha256 in
   `artifacts/slice63_sha256_manifest.txt`) rather than from an earlier parent
   would stop this recurring.
2. **The funding seam at `2026-08-09T16:00Z` is still empty.** This extension
   did not fill it. It changes no number yet and will eventually feed a stale
   rate to a decision.

## What would move this forward

Five more **closed daily bars** — not funding prints. At six, the first forward
trade can close, if the rule signals at all. The gate wants 20 closed forward
trades and 180 days; neither number moved, and neither should have.

**The pilot has one post-`t1` day — the same one it had last slice — and zero
forward observations.**
