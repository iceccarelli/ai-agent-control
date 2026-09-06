# SLICE 57 — VERDICT

```
SLICE57_VERDICT:                    PASS
funding_carry_fade_v1_still_frozen: YES
signal:                             funding_carry_fade_btc_v1
folds_locked_before_oos:            YES
oos_n:                              41
oos_control:                        VALID
oos_M1:                             95.13
oos_M2:                             96.00
oos_mean_R:                         +0.1736
q1_mean_R:                          +0.3164   (21 trades)
q2_mean_R:                          +0.0236   (20 trades)
edge_verdict:                       POSITIVE
cleared_edge_signal:                funding_carry_fade_btc_v1
slice55_btc_artefact_unedited:      YES
FUND_ABS_unchanged:                 YES
Model/live still BLOCKED:           YES
Closer to autonomous profit agent?: YES — in the precise and only sense
                                    the intake defines, and no further
```

## Evidence table

| claim | observed | file / command |
|---|---|---|
| baseline clean before any change | 3,645 passed, 1 failed (a repo test defect, below), 1 skipped; FROZEN_ABSENT = 11; cleared_edge null | `artifacts/slice57_baseline_pytest.log` |
| design note precedes any signal code | EDGE §39 commit `--stat` lists `EDGE.md` only; `signals/` untouched | `git show --stat 40c9d1e` |
| fold calendar locked before the module existed | folds commit precedes module commit in history | `test_git_ordering_puts_the_lock_before_the_signal_module` |
| the cut has no free parameters | `method = index_midpoint_50pct`, `mid_index = 730 = 1461 // 2` | `artifacts/funding_carry_fade_btc_v1_folds.json` |
| the cut has not moved since | folds sha256 `ff5cc8a2…013c` still matches the lock log | `artifacts/slice57_folds_lock.log`, `test_the_calendar_still_hashes_to_what_was_locked` |
| both source corpora pinned and unchanged | linear `3c2e8601…`, funding `9f5ceefc…` still match disk | `test_the_pinned_hashes_still_match_the_files_on_disk` |
| universe is BTC only, enforced | 8 rejected spellings incl. ETHUSDT/SOLUSDT; no widening path exists | `TestTheUniverseIsBtcOnly` (7 tests) |
| nothing was retuned | 9 constants identical to the frozen family; 9 functions are the SAME OBJECTS | `TestTheConstantsAreTheIntakesAndTheFrozenFamilys`, `TestTheRuleIsNotReimplemented` |
| the product reproduces the frozen family exactly | full-sample mean net R `+0.09637759630974421` — byte-identical to slice 55's | `artifacts/slice57_diagnostic_fullsample_BTCUSDT.json` |
| the full-sample diagnostic cannot register | `registration_eligible: false`; refused even with an attestation attached | `test_the_full_sample_diagnostic_cannot_register` |
| OOS count taken before any control | n_OOS = 41 against a floor of 40, five loss points printed | `artifacts/slice57_oos_count_finding.log` |
| the 40-trade floor cannot be lowered from a shell | module constant, no CLI argument | `tools/count_funding_carry_fade_btc_oos.py` |
| OOS control run on the OOS window only | 200/200 surrogates, 41 trades each — matching the real schedule | `artifacts/slice57_oos_control_BTCUSDT_n1000.log` |
| control VALID under the slice-37 clauses | z +1.121, KS p 0.1561, 0.0% incomplete | same log |
| rotations confined to the OOS window | `eligible bars : 724` after intersection | `artifacts/slice57_oos_edge_BTCUSDT.log` |
| one OOS run, no re-seed, no grid | `runs_executed_on_oos: 1`, `grid_or_sweep_run: false` | `artifacts/slice57_stage1_funding_carry_fade_btc_v1.json` |
| all five clauses recorded and met | `gate_clauses` block, five entries, all `met: true` | `artifacts/slice57_oos_edge_BTCUSDT_summary.json` |
| **M1 cleared by three replicates** | 73 of 1,500 above observed; 76 would fail it; headroom 2.4% of the mean | `HOW_FRAGILE_THIS_RESULT_IS.m1_margin` |
| **the effect decays toward the present** | OOS Q1 +0.3164 → Q2 +0.0236 | `artifacts/slice57_oos_stability_Q1Q2.json` |
| **three trades carry 41% of the total** | `top_3_trades_share_of_total_r = 0.411` | same |
| only the OOS artefact can register | 9 clauses broken in turn, each refuses; unbroken, it registers | `artifacts/slice57_registration_discipline.log` battery 3 |
| a relabelled slice-55 POSITIVE cannot register | refused | `test_the_relabelled_slice55_artefact_cannot_register` |
| a claim scored under a different cut cannot register | fold-hash mismatch refuses | `test_a_claim_scored_under_a_different_cut_cannot_register` |
| clause 5 is enforced in code, not just observed | mean R of −0.05 and of 0.0 both refuse | `test_a_negative_mean_net_r_cannot_register`, `test_a_zero_mean_net_r_cannot_register` |
| slice-55 BTC artefact unedited | md5 `264292711334d724b675c8a8a3734fb6`, unchanged since slice 55 | `md5sum` |
| the frozen family is still frozen | `funding_carry_fade_v1` in `ABSENT_SIGNALS`, status ABSENT, 11 names | `test_the_family_it_derives_from_is_still_frozen` |
| eleven forgeries still refused | battery 1, 11 of 11 REFUSED | `artifacts/slice57_registration_discipline.log` |
| the clear arms nothing | `models/current` absent, POLICY_MODE off, live false, paper | `tools/print_project_status.py` |
| no trading path reads the field | six modules walked, none names `cleared_edge_signal` | `test_no_trading_path_consults_the_cleared_edge_field` |
| the NO EDGE CLAIM line was not weakened | unchanged string; status exits 0 with an ADVISORY naming both facts | `test_the_no_edge_claim_line_was_not_weakened` |
| paper certification green | exit 0 | `artifacts/slice57_paper_cert.log` |
| full suite green | **3,732 passed, 1 skipped** | `artifacts/slice57_pytest.log` |
| every slice-57 artefact hashed | 20 entries | `artifacts/slice57_sha256_manifest.txt` |

## The three things a reader should not skip

**1. It cleared by three replicates and one trade.** M1 95.13 against 95.0 means
73 of 1,500 rotations beat the observed mean and 76 would have failed it. n_OOS
41 against a floor of 40. Both margins were predicted as *likely to be thin* in
EDGE §39c before the module existed, which is the only reason the fold lock can
be believed rather than merely asserted.

**2. It is a time split of already-observed data.** The late window was part of
the sample slice 55 measured to 97.0 / 97.5 — the reading that motivated writing
this product. Nobody chose these constants blind to this window. The honest
description is *survived a pre-declared time split of the data it was found in*,
not *validated on unseen data*. A forward paper period is the obvious next ask.

**3. The most recent half of the OOS window is flat.** +0.3164 on the first 21
trades, +0.0236 on the last 20, continuing a monotone decay visible across the
full-sample folds. This cannot fail the gate — the intake lists five clauses and
none of them is a stability test, and adding a sixth after seeing the numbers
would be moving the goalposts against a result. It is the single most important
thing to weigh before trading this.

## One defect found in the baseline, and two things refused

The baseline suite had **one failure**: `test_no_frozen_family_is_presented_as_an_open_thesis`
matched two literal phrases and went red on the human's own intake, which says
*"FROZEN ABSENT (11th line) … **Do not reopen.**"* — the right thing in the
author's words. EDGE §12c for the sixth time. Replaced with a **line-scoped,
vocabulary-based** check that is strictly stronger: the old form let one
"COMPLETED — CLOSED" anywhere in the header cover *every* frozen name mentioned.

**Refused: adjusting M1 for the control's 52.30 offset.** The three-clause
control rule passed, and there is no pre-declared adjustment. Inventing one after
seeing 95.13 would be moving the goalposts against a result — as dishonest as
moving them toward one.

**Refused: treating the Q1/Q2 decay as a sixth clause.** Same reason, same
direction of dishonesty.

---

OOS is the only registration gate.
Slice-55 BTC 97 did not clear this product.
Closer to a bank-grade autonomous profit agent is YES only if
`cleared_edge_signal == funding_carry_fade_btc_v1` under OOS rules.
