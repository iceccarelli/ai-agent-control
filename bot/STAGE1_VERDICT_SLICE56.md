# SLICE 56 — VERDICT

```
SLICE56_VERDICT:                    PASS
funding_carry_fade_v1_frozen:       YES
freeze_reason:                      FAMILY_ABSENT_MULTI_SYMBOL_RULE_FAILED
btc_positive_artefact_preserved:    YES  (97.0 / 97.5, n=85, control VALID)
ETH_ABSENT:                         YES  (47.7 / 48.5, n=76, control VALID)
SOL_ABSENT:                         YES  (2.3 / 1.0, n=162, control VALID,
                                          strongly negative: mean net R
                                          -0.1658, M2 delta -0.1315,
                                          CI [-0.1402, -0.1229])
forged_POSITIVE_refused:            YES  (all 11 names; and a 3-symbol forgery
                                          that SATISFIES the universe rule)
FROZEN_ABSENT_count:                11
ProjectStatus.cleared_edge_signal:  null
Model/live still BLOCKED:           YES
new_signal_invented:                NO
Closer to autonomous profit agent?: NO
```

## Evidence table

| claim | observed | file / command |
|---|---|---|
| baseline was clean before any change | 3,602 passed, 1 skipped; `ABSENT_SIGNALS` = 10; funding **not** frozen | `artifacts/slice56_baseline_pytest.log`, commit `ccfe852` |
| design note precedes code | EDGE.md §38 committed at `966d27b`; deny-list first touched at `b75d568` | `git log --oneline`, `EDGE.md` §38 |
| family frozen ABSENT | `"funding_carry_fade_v1" in ABSENT_SIGNALS`; `FROZEN_STATUS[...] == "ABSENT"` | `project_status.py`, `tests/…::TestTheSlice56FreezeOfFundingCarryFade` |
| count is 11 and consistent | `len(ABSENT_SIGNALS) == 11 == len(FROZEN_ABSENT) == len(FROZEN_STATUS)` | `test_the_deny_list_is_eleven_and_internally_consistent` |
| evidence cites all three readings | `97.0/97.5`, `47.7/48.5`, `2.3/1.0`, `85`, `76`, `162`, `95.0` all present | `test_the_evidence_carries_all_three_readings_and_their_counts` |
| evidence cites every control | `+0.632`, `+0.168`, `+1.933`, `0.0% incomplete`, `control_validated true` | `test_the_evidence_records_that_every_control_passed` |
| evidence does not hide the BTC POSITIVE | `EDGE_EVIDENCE_POSITIVE`, `RETAINED UNEDITED`, artefact path all present | `test_the_evidence_does_not_hide_the_btc_positive` |
| evidence refuses "almost POSITIVE" | `NOT AN 'ALMOST POSITIVE'`, `disagree in SIGN`, `14%` present | `test_the_evidence_refuses_the_almost_positive_reading` |
| BTC artefact byte-identical | md5 `264292711334d724b675c8a8a3734fb6`, same as slice-55 baseline | `md5sum artifacts/slice55_edge_funding_carry_fade_BTCUSDT_summary.json` |
| ETH / SOL artefacts byte-identical | `1c9509a3…`, `4c964d75…` — unchanged | same command |
| BTC artefact still says POSITIVE | `verdict == "EDGE_EVIDENCE_POSITIVE"`, both bars passed | `test_the_btc_artefact_still_says_positive` |
| single-symbol forgery refused | hook returns `None` | `test_a_perfect_single_symbol_forgery_is_refused` |
| **3-symbol forgery satisfying the universe rule refused** | hook returns `None` | `test_a_perfect_forgery_that_SATISFIES_the_universe_rule_is_refused` |
| …and that forgery *would* have registered without the freeze | hook returns `"funding_carry_fade_v1"` with the name lifted | `test_that_forgery_would_have_registered_without_the_freeze` |
| refused twice over, each guard sufficient alone | universe lifted → `None`; freeze lifted → `None`; both lifted → registers | `artifacts/slice56_registration_discipline.log`, battery 3 |
| every frozen name refuses a perfect forgery | 11 of 11 REFUSED | `artifacts/slice56_registration_discipline.log`, battery 1 |
| real `artifacts/` registers nothing | `cleared_edge_signal_from_artifacts("artifacts") is None` | `test_the_real_artifacts_directory_still_registers_nothing` |
| constants untouched | `(0.0001, 1.5, 1.0, 1.5, 5, 14, 1, 25.0, "next_open")` | `test_the_signal_constants_are_untouched` |
| universe untouched | `(("BTCUSDT","ETHUSDT","SOLUSDT"), 2)` | `test_the_universe_rule_was_not_narrowed_to_the_symbol_that_passed` |
| nothing re-measured | no `slice56_*edge_funding*` or `slice56_*control_funding*` artefact exists | `test_no_new_edge_or_control_artefact_was_produced_for_this_family` |
| intake closed, next slot empty | `COMPLETED — CLOSED` + `WAITING — EMPTY`, all fields `(empty)` | `NEW_SIGNAL_INTAKE.md` |
| no signal invented | `signals/` unchanged; every module frozen or named in the intake | `test_every_implemented_signal_is_frozen_or_named_in_the_intake` |
| ledgers agree with the code | eleven everywhere; spelled-out counts derived from `len(ABSENT_SIGNALS)` | `test_the_hold_counts_every_frozen_name`, `test_the_runbook_frozen_count_matches_the_code` |
| freeze artefact present and honest | schema `research_freeze/1`, 11 fields cross-checked against live code | `artifacts/slice56_funding_carry_fade_freeze.json`, `TestTheSlice56FreezeArtefact` |
| paper certification green | exit 0, `NO EDGE CLAIM` on every surface | `artifacts/slice56_paper_cert.log` |
| status truthful | `CLOSED` / `null` / `paper` / `live false` / `POLICY_MODE off` | `python3 tools/print_project_status.py` |
| no model, no live | `models/current` absent | `artifacts/slice56_registration_discipline.log` |
| full suite green | **3,646 passed, 1 skipped** | `artifacts/slice56_pytest.log` |

## What was refused

* **No re-run.** Not one edge measurement, not one control, not one count. Every
  number in this slice was copied from slice 55's artefacts.
* **No retune.** `FUND_ABS` is 0.0001, the stop is 1.5 ATR, the take profit is
  1.0 R, the horizon is 5, and the cost model still charges funding over the
  hold.
* **No universe narrowing.** `{BTCUSDT, ETHUSDT, SOLUSDT}` with a minimum of 2,
  exactly as pre-declared. Dropping the two symbols that failed would have made
  a rule out of a result.
* **No BTC-only promotion**, in any form, by any route.
* **No deletion.** The inconvenient artefact is still there, still saying
  `EDGE_EVIDENCE_POSITIVE`, byte-for-byte.
* **No new signal, no filled intake, no model, no live arming.**

## Two notes worth keeping

**The freeze is not redundant with the universe rule.** A forger who supplies all
three symbols satisfies `MULTI_SYMBOL_MINIMUMS` completely. The freeze is what
refuses that, and there is a test that proves the same forgery registers once the
name is lifted from the deny-list.

**Battery 3 failed first, correctly.** Its proof that the universe rule was
load-bearing depended on that rule being the *only* thing refusing the artefact.
The freeze broke that assumption, and the tool reported BREACHED rather than
printing PASS on a check that had gone vacuous. It now lifts each guard
separately and prints all four readings. A tool that notices its own obsolescence
is worth more than one that keeps agreeing with itself.

---

This slice closes a measured family. It does not create edge.
Closer to a bank-grade autonomous profit agent remains NO
until a future human thesis clears Stage 1 under rules declared before the run.
