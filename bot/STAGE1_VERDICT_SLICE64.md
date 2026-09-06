# SLICE 64 — VERDICT

```
SLICE64_VERDICT:                    PASS_WITH_DEFECTS
extension_present:                  YES
after_t1_linear_bars:               2
after_t1_dates:                     ['2026-08-10', '2026-08-11']
linear_last:                        2026-08-11T00:00:00+00:00
linear_rows:                        1463
after_t1_funding:                   9        funding_last: 2026-08-12T16:00:00+00:00
new_linear_bars_since_slice63:      1
the_window_grew:                    YES      <-- first genuine growth since t1 was locked

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
        final     0 failed / 4,191 passed / 2 skipped
```

**Why `PASS_WITH_DEFECTS` and not `PASS`.** Every PASS condition in the mission
is met: growth verified from files, forward segment honestly computed, ceiling
declared and respected, gate refusing, clear intact, pack frozen, suite green,
no live or model theatre. But the tree shipped onward still contains **four
false manifest assertions** that this slice deliberately did not repair. Calling
that a clean PASS would understate what a downstream reader needs to know. The
defects are inherited from the pack, taint no claim, and are pinned by tests.

## The window grew, and the answer did not change

Both sentences are true and neither may be dropped.

```
after_t1_linear    1 -> 2      ['2026-08-10', '2026-08-11']
linear rows     1462 -> 1463
funding rows    4389 -> 4392

closed forward bars                          2
closed forward bars needed for one trade     6     (HORIZON 5 + next_open entry)
max_possible_forward_closed_trades           0     = max(0, 2 - 5), declared in §47d before the run
observed                                     0
```

Slice 63 had to report that nothing had moved; reporting the same thing here
would be as dishonest as reporting progress there. **The evidence window is
doubling in size while producing no evidence.**

One finding is sharper than slices 62–63 could produce. There, `2026-08-10`'s
zero was **doubly determined** — no funding setup *and* it was the last bar of
the corpus, which `directed_signal_bars` excludes on its own. A second bar has
arrived, so the structural exclusion no longer covers it and **the funding
reason stands alone**: the appended prints run 7.5e-05 … 6.6e-05, all inside
`FUND_ABS = 1e-4`. The rule genuinely declined to signal.

## Defect 1 — a manifest that asserts data which does not exist

The human updated both MANIFESTs this slice by **broadcasting BTCUSDT's counts
and end dates across all three symbols**:

```
file                                 manifest   on disk   verdict
BINANCE_LINEAR_BTC_USDT_1D.csv.gz        1463      1463   ok
BINANCE_LINEAR_ETH_USDT_1D.csv.gz        1463      1461   FALSE
BINANCE_LINEAR_SOL_USDT_1D.csv.gz        1463      1461   FALSE
BINANCE_LINEAR_BTC_USDT_FUNDING.csv.gz   4392      4392   ok
BINANCE_LINEAR_ETH_USDT_FUNDING.csv.gz   4392      4383   FALSE
BINANCE_LINEAR_SOL_USDT_FUNDING.csv.gz   4392      4458   FALSE — UNDERSTATES by 66
```

ETH's and SOL's four files are byte-identical to slice 63's; they were not
extended at all. **SOL funding is false in the worse direction** — it holds 4458
prints, the *previous* manifest said 4458 correctly, and this edit replaced a
right number with a wrong one.

Four of the twelve baseline failures are this being caught by guards slice 55
already had. That is the system working.

**Not edited, deliberately.** Rewriting a data-provenance record to state
whatever the files say would be asserting a provenance this programme cannot
attest — nothing here knows independently that ETH *should* hold 1461 rows
rather than having lost two — and a manifest that agrees with disk by
construction certifies nothing. The four claims are enumerated by name, with a
sweep that would catch a fifth hiding behind them, and the enumeration goes red
the moment any of them moves — including when a human repairs them.

**No claim here is tainted:** BTC's two entries are accurate, the prefix pins
hold, and every count reported is read from the corpus, asserted by a test that
compares the artefact's figures against `corpus_prefix` directly.

## Defect 2 — second consecutive pack regression, same parent

`tradingbot_slice64_dataready.zip` is the slice-61 tree again. The same three
digest probes read slice-61 (`EDGE.md 2af62040…`, `signals/… 2668e74b…`,
`tests/test_slice55… 12da76a1…`), and everything from slices 62 **and** 63 is
absent. Restored byte-identically; dated records restored rather than
regenerated — checkable, because each carries a `git_commit` absent from this
repository's history.

## The conflation this exposed

Slice 62 asserted `pinned_rows == manifest_rows`, treating the manifest as a
description of the measured prefix. It was — **by accident**: the manifest was
stale at exactly the prefix length. An agreement that holds for one moment is
not a relationship, and slice 64's manifest update ended it.

| record | kind | describes | asserted by |
|---|---|---|---|
| `artifacts/slice55_data_eligibility.json` | dated | the measured prefix | `corpus_prefix.check` |
| `data/*/MANIFEST.json` | living | the file on disk | the manifest audit |

Strictly stronger than before: the old form could be satisfied by a manifest and
an artefact agreeing with each other while **both** disagreed with the files.

## The lesson, for the fourth time

Four test files pinned a **live absolute** — `appended_rows == 1`, `== 2`,
`== 6`, a byte-identity with slice 62's corpus. Each was true when written and
false the moment the window moved, which is the event the programme waits for.
Slices 54, 57, 62 and 63 each unpicked a version of this.

The rule, now written down: **assert the dated claim against the frozen
artefact; assert only the direction of travel against live disk.**

A consequence: slice 63's `test_every_restored_file_matches_its_recorded_digest`
also re-verified the restored *apparatus* against live files, which a later
slice may legitimately amend — and this one did, three times. A dated manifest
cannot be the authority on files still being worked on. It now verifies only
what is frozen; the **current** slice's manifest verifies the live apparatus,
because that is where an amendment can be declared beside its reason. All three
amendments carry both digests, a why, and an argument for why the new form is
stronger.

## Evidence table

| claim | observed | file / command |
|---|---|---|
| window grew | `after_t1_linear` 1 → 2 | `artifacts/slice64_data_freshness.json` |
| growth is real, not the note | prefix digests match slice-57 pins; append-only | `artifacts/slice64_data_freshness.json` |
| note quotes compressed digest | `116eef53…` matches the gz file, not the pin | `artifacts/slice64_data_freshness.json` |
| open bar absent | `2026-08-12` not present; closes tomorrow | `test_the_open_bar_for_today_is_absent` |
| ceiling still 0 | `max(0, 2 − 5)`, declared §47d before the run | `artifacts/slice64_forward_shadow.json` |
| forward trades | 0, within ceiling | `artifacts/slice64_forward_shadow.json` |
| rule stood aside on both bars | no funding setup; 08-10 no longer last-bar | `artifacts/slice64_forward_shadow.json` |
| scoring not re-tuned | AST-equal per function vs slice 63 | `test_the_scoring_was_not_re_tuned_since_slice63` |
| manifest defect | 4 of 6 entries false; SOL understates by 66 | `TestTheManifestsDescribeTheFilesBesideThem` |
| BTC entries accurate | both agree with their files | `test_the_measured_products_entries_are_accurate` |
| dated records not regenerated | their commits absent from this history | `test_the_dated_records_were_not_regenerated_on_this_tree` |
| gate refuses | 2 / 8, `allows_live` false | `artifacts/slice64_promotion_gate.json` |
| OOS untouched | `28b7dfe0…`; folds `ff5cc8a2…` | `test_the_oos_clear_was_not_re_scored` |
| registration discipline | HELD | `artifacts/slice64_registration_discipline.log` |
| paper session | green | `artifacts/slice64_paper_cert.log` |
| suite | 4,191 passed / 0 failed / 2 skipped | `artifacts/slice64_pytest.log` |

## Forbidden list — every item, answered

| forbidden | this slice |
|---|---|
| synthetic / invented / forward-filled bars | `bars_fabricated: 0`; no corpus written by any tool |
| relabelling bars ≤ `t1` as forward | `in_forward_window` strict at `t1`; pinned by tests |
| trusting the note without verifying | every claim re-derived from hashes; note's digest identified as compressed |
| re-scoring OOS / Stage-1 | artefact byte-identical; `cleared_edge_re_scored_this_slice: false` |
| changing constants, schedule, caps, thresholds | fingerprint `662de011…`; AST-equal scoring |
| raising `max_notional_usd` above 100 | 100.00, asserted |
| arming live, `models/current`, training | live dark; `policy_mode` off; no model |
| reopening any of the 11 frozen families | 11 unchanged; ETH/SOL not measured — least of all now |
| `closer_to_autonomous_profit_agent = true` | false |
| shadow mean R as Stage-1 evidence | `forward_mean_net_r: null`; both flags false |
| completing human checklist items in code | `human_items_completed_in_code: 0` |
| clamping unfinished exits | `exit_clamping_to_corpus_end: false` |
| slicing a tiny post-`t1` window | `bar_array_sliced: false`; flags zeroed |
| claiming growth when `after_t1_linear` is 1 | it is 2, from disk, and 1 → 2 is recorded as the delta |
| claiming observation when trades are 0 | `is_forward_observation: false` |
| rebranding historical M4 WARN | forward M4 `INSUFFICIENT_DATA`; WARN stands unerased |

## Three things carried forward for the human

1. **Repair the MANIFESTs.** Four entries assert data that does not exist; SOL
   funding should read 4458. When fixed, the enumeration in
   `TestTheManifestsDescribeTheFilesBesideThem` goes red — that is the signal to
   retire it, not a regression.
2. **Build slice 65 from `tradingbot_slice64.zip`.** Two consecutive packs have
   been the slice-61 tree.
3. **The funding seam at `2026-08-09T16:00Z` is still empty**, three slices on.

## What would move this forward

**Four more closed daily bars** — six is the arithmetic minimum. And then the
rule must actually signal, which on both available forward days it did not, for
want of a funding rate above `1e-4`. The gate wants 20 closed forward trades and
180 days; neither moved, and neither should have.

**The pilot has two post-`t1` days and zero forward observations.**
