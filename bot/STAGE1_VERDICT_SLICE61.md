# SLICE 61 — VERDICT

```
SLICE61_VERDICT:                    PASS
extension_present:                  NO
forward_observations_to_date:       0
is_forward_observation:             false
oos_re_scored:                      NO
monitor_status:                     not re-run this slice — slice 59's WARN stands
promotion_gate_allows_live:         NO
closer_to_autonomous_profit_agent:  NO
```

## What happened

The human pack named two extended data files and **contained neither**.

```
tradingbot_slice61_dataready.zip   2,474 bytes
sha256 b13a2586510892e483c60c929b33dbcaa24582ba164302bd319790e30e59151d
    4219  MISSION_SLICE61.md
     676  HUMAN_DATA_NOTE_SLICE61.md
  members 2   data files 0   the note names 2 extended files
```

This is recorded without accusation. A pack can be assembled and its large
members fail to attach; that is routine and says nothing about intent. What
matters is only that the artefact which would settle the question is absent, so
the question is not settled.

## The check that settled both of the note's promises at once

```
linear  sha256 == the pin locked in slice 57   TRUE
funding sha256 == the pin locked in slice 57   TRUE
```

**Identical hashes prove two things simultaneously, in opposite directions:**

* **"no rewrite of history before t1" is KEPT** — exactly and provably. Not one
  byte of measured history has moved. This is the more important of the two
  promises and it holds;
* **"extended files" is NOT present** — identical files cannot contain new rows.

One hash comparison settles both. That is the neatest available demonstration of
why the check is a hash and not a reading of the sentence.

## Evidence table

| claim | observed | file / command |
|---|---|---|
| baseline all-true before any change | 23/23 rows; 3,993 passed, 2 skipped | `artifacts/slice61_baseline_pytest.log` |
| design note precedes all code | EDGE §44 commit `--stat` lists `EDGE.md` + baseline log only | `git show --stat` |
| the pack is pinned by hash | `b13a2586…9151d`, 2,474 bytes | `slice61_data_freshness.json → human_pack` |
| **the pack contained 0 data files** | 2 members, both `.md` | `test_the_pack_contained_no_data_files` |
| the note claims 2 extended files | recorded verbatim | `test_the_note_claims_two_extended_files` |
| the discrepancy is stated without accusation | "fail to attach", "not settled" | `test_the_discrepancy_is_recorded_without_accusation` |
| **no history was rewritten** | both hashes == slice-57 pins | `test_both_files_still_hash_to_their_slice57_pins` |
| **no extension exists** | same fact, other direction | `test_extension_present_is_false` |
| last linear bar is still `t1` | 2026-08-09T00:00:00+00:00 | `test_the_last_bar_is_still_t1` |
| 0 new closed bars | `new_linear_bars_count: 0` | `slice61_data_freshness.json` |
| the tool hashes files, never parses the note | no `read_note` / `parse_note` | `test_the_freshness_tool_reads_files_not_the_note` |
| no fetch attempted, with a reason | slice 60's `ProxyError` cited; "activity, not evidence" | `test_no_fetch_was_attempted_and_the_reason_is_given` |
| nothing fabricated | `bars_fabricated: 0`, `corpus_appended_to: false` | `test_nothing_was_fabricated` |
| **HOLD, zero forward** | `result: HOLD`, `is_forward_observation: false`, `0` | `test_it_says_HOLD_with_zero_forward` |
| the flag tracks the count, not the bars | `is_forward_observation == (count > 0)` | `test_is_forward_observation_tracks_the_count` |
| **monitors not re-run** | `monitors_re_run: false`, readings `null` | `test_the_monitors_were_not_re_run` |
| slice 59's readings match exactly | status and M-4 detail identical | `test_the_readings_that_stand_match_slice59_exactly` |
| **M-4 still WARN, not silenced** | earlier +0.4669 on 17, recent −0.0798 on 18 | `test_m4_was_not_silenced` |
| the 1 funding print still produces nothing | decision clock is the daily bar | `test_the_single_funding_print_still_produces_nothing` |
| fingerprint unchanged | `662de0115880871352d5d623b1020eaa` | `test_the_fingerprint_is_the_declared_one` |
| caps unchanged | 1 / 1 / 100.00 | `test_the_caps_are_untouched` |
| schedule unchanged | `one_entry_per_contiguous_run` | `test_the_schedule_is_still_one_per_run` |
| thresholds unchanged | M1 −0.25, M3 0.60, all four | `test_the_thresholds_are_untouched` |
| folds not re-cut | hash matches lock log; `t1` unchanged | `test_the_folds_were_not_re_cut` |
| clear not re-scored | no slice-61 edge/control/summary artefact | `test_the_clear_was_not_re_scored` |
| the HOLD cannot register | refused even renamed `x_summary.json` | `test_even_renamed_as_a_summary_it_registers_nothing` |
| clear still from slice 57 | n=41, M1 95.13, M2 96.0, control true | `test_the_clear_still_comes_from_slice_57` |
| eleven freezes, forgeries refused | 11/11 | `artifacts/slice61_registration_discipline.log` |
| **gate still refuses** | `allows_live: false`, 2 of 8, unchanged | `TestTheGateStillRefuses` |
| live dark, model absent | live false, policy off, paper | `artifacts/slice61_project_status.json` |
| paper certification green | exit 0 | `artifacts/slice61_paper_cert.log` |
| full suite green | **4,044 passed, 2 skipped** | `artifacts/slice61_pytest.log` |
| artefacts and human docs hashed | 10 entries | `artifacts/slice61_sha256_manifest.txt` |

## The third instance of one lesson

| slice | the substitution | what it would have cost |
|---|---|---|
| 55 | a multi-symbol rule written in **prose** instead of code | a genuine single-symbol POSITIVE registered a cleared edge |
| 59 | **history** relabelled as forward experience | a replay would have satisfied the gate's central item |
| 61 | a **note asserting** data, in place of the data | zero bars counted as an extension |

The general form: **a claim about the world is not the world.** The defence is
the same each time and it is not sophistication — it is arithmetic. Check the
thing itself. Here that meant hashing two files instead of reading one sentence.

## What is actually needed to move this chain

Not a note, and not another slice. **Bars that did not exist when the rule was
measured.** The corpus ends at `t1`; the promotion gate needs 20 closed forward
trades across 180 days; at the pilot's rate that is roughly fourteen months of
real time. A data pack containing the two named `.csv.gz` files, with timestamps
strictly after 2026-08-09 and `synthetic: false`, would let slice 62 count
genuine forward observations for the first time.

---

Zero or more true forward bars must be stated honestly.
This slice does not deploy an autonomous profit agent.
Promotion remains REFUSE.
