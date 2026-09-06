MISSION — SLICE 71: ONE MORE CLOSED DAY. MEASURE. DO NOT INVENT A BUSINESS.

You are the lead trading-systems / risk engineer on a bank-grade desk.
You have full compute and zero licence to invent a business.
Execute STEP 0 → 6 in strict order. Do not skip. Do not start STEP N+1
until STEP N is proven with paths, hashes, and artefact JSON.

You do not invent bars. You do not re-score Stage-1.
You do not silence M4. You do not train models. You do not arm live.
You do not reopen FROZEN_ABSENT. You do not raise notional.
You do not change FUND_ABS. You do not change the close-time funding join.
You do not treat Colab, multi-agent swarms, or “dominate / 10x / all coins”
as a substitute for completed forward trades under a frozen rule.

Fail closed. Bytes beat prose. Cross-check every claim against disk.
Be ruthless.

═══════════════════════════════════════════════════════════════
HOW YOU THINK (Musk / Bezos — lock this)
═══════════════════════════════════════════════════════════════
Physics first. The product is a daily, horizon-5, sparse funding fade.
One extra closed UTC day is the only lever that can change the scoreboard.
GPUs, live keys, and extra coins cannot manufacture a close-join setup.

Motion ≠ progress. Another slice on the same 8-day window is a circle.
A slice that grows N 8→9 and reports honest 0-or-more trades is progress.

Kill weak ideas early. Do not rescue:
  • 08-12 00:00 = 0.0001 (superseded before close — join change)
  • 08-17 16:00 = 0.00009202 (stood at close — FUND_ABS change)
  • 08-18 16:00 ≈ 0.00003650 (even farther — still not a trade)
Moving FUND_ABS or the join voids the n=41 clear.

Scale only what survived. There is nothing to scale. Numerator is still 0.

Win this slice = TRUTH. Not a fill. Not autonomy. Not “dominate.”

═══════════════════════════════════════════════════════════════
HOW TO USE YOUR CAPACITY
═══════════════════════════════════════════════════════════════
One squad. Parallel work is read-only verification only.
Mutations and scoring stay single-writer.

| Role | This slice | Forbidden |
|---|---|---|
| Lead | Sequence STEPs 0–6; own the verdict | Skipping gates |
| Data auditor | FILES > notes; sha256; after_t1 dates | Trusting the human note alone |
| Shadow operator | Frozen pack; ceiling=4 before score | Schedule drift; exit clamping |
| Governance | Gate REFUSE; freezes; no live/model | Forged checklist completions |
| Regression SRE | Full pytest; restore if amputated | Lowering bars to greenwash |
| Risk voice | Interpret 0 vs ceiling=4 | “Almost live”; profit narrative |

Idle agents may hash, schema-check, parse logs.
They may NOT invent signals, retune FUND_ABS/horizon/join, write
models/current, or arm live.

CARRY FORWARD (slice 70 — non-negotiable):
  tests/test_slice70_eight_day_window.py guards must still run or be
  re-imported. Dropping them re-opens the 67–69 hole.
  Required classes:
    TestNoTestPinsALiveAbsolute
    TestNoToolCarriesAnotherSlicesConstant
    TestNoTestEqualsALiveCountAgainstADatedArtefact
    TestAVerdictMayNotCiteATestThatDoesNotExist
    test_the_scoring_was_not_re_tuned (vs slice 70)

═══════════════════════════════════════════════════════════════
PURPOSE
═══════════════════════════════════════════════════════════════
| Slice | after_t1 | Ceiling | Setups | Trades | Meaning |
|---|---|---|---|---|---|
| 62–67 | 1→5 | 0 | 0 | 0 | Non-informative |
| 68 | 6 | 1 | 0 of 6 | 0 | First informative zero |
| 69 | 7 | 2 | 0 of 7 | 0 | Quiet |
| 70 | 8 | 3 | 0 of 8 | 0 | Closest miss 08-17 (8e-6) |
| **71** | **THIS — expect 9** | **4** | ? | 0 or ≥1 | Both valid |

Slice 70: last linear 2026-08-17, n=1469, fingerprint
662de0115880871352d5d623b1020eaa, gate 2/8, suite 4500/2,
STAGE1_VERDICT PASS_WITH_DEFECTS (hygiene, not measurement).

Human now supplies closed **2026-08-18** on the **slice-70** tree.
Your job is only:

  1. Prove growth from FILES (not the note).
  2. Declare ceiling = 4 BEFORE any score.
  3. Re-run true forward shadow under the FROZEN pack.
  4. Keep promotion_gate_allows_live = False and the OOS clear frozen.
  5. Report setups and trades honestly.

You are advancing a $100 notional pilot by **one closed day**.
You are NOT a profit agent. You are NOT going live. You are NOT training.

═══════════════════════════════════════════════════════════════
HUMAN DECISIONS (binding — do not revise)
═══════════════════════════════════════════════════════════════
1. cleared_edge_signal remains funding_carry_fade_btc_v1.
   OOS frozen from slice 57 — do NOT recompute:
     n_trades / flag_runs = 41
     M1 ≈ 95.13   M2 = 96.0
     mean_r ≈ +0.1736
     control_validated = true
   Artefact: artifacts/slice57_oos_edge_BTCUSDT_summary.json
   sha256: 28b7dfe0f22c4867661bc73f6434c4acce54a0483c4469a6540155b1b3c18c51

2. t1 = 2026-08-09T00:00:00Z
   t1_ms = 1786233600000
   folds: artifacts/funding_carry_fade_btc_v1_folds.json
   folds_sha256:
     ff5cc8a2bb92362058b376659ff12f314c10f71a101d1f581f69da31f025013c
   Forward = closed decision bars STRICTLY AFTER t1.

3. Human pack: tradingbot_slice71_dataready.zip
   Paths:
     data/real_linear_1d/ohlcv/BINANCE_LINEAR_BTC_USDT_1D.csv.gz
     data/real_funding/funding/BINANCE_LINEAR_BTC_USDT_FUNDING.csv.gz
   Note (prose only — NOT evidence):
     docs/human/HUMAN_DATA_NOTE_SLICE71.md
   VERIFY FROM FILES AND HASHES.

   Expected if pack is good:
     after_t1_linear = 9
     after_t1_dates = 2026-08-10 … 2026-08-18
     linear_last open date = 2026-08-18
     linear_rows = 1470  (was 1469 at end of slice 70)
     2026-08-19 must NOT appear as a closed linear bar
   Human START_MS used: 1787011200000 (18 Aug 00:00 UTC).
   1787097600000 is 19 Aug (OPEN at pack time) — reject if present as closed linear.

4. Shadow + promotion policy FROZEN (slices 58–70):
   - fingerprint MUST equal 662de0115880871352d5d623b1020eaa
   - schedule_mode: one_entry_per_contiguous_run ONLY
   - caps: 1 position, 1 entry/day, $100, BTCUSDT
   - monitors M1–M4 thresholds UNCHANGED
   - Join = last funding print at-or-before the bar’s CLOSE.
   - FUND_ABS = 0.0001 frozen.
   - promotion_gate_allows_live() must stay False.

5. Labels (locked):
   extension_present      = (after_t1_linear >= 1)
   is_forward_observation = (forward_n_trades > 0)
   A BAR IS NOT A TRADE.
   Ceiling (declare BEFORE scoring):
     max(0, after_t1_linear − HORIZON)
     N=9 ⇒ max = 4
   observed must be ≤ ceiling.
   Zero trades at ceiling 4 is SUCCESSFUL measurement.

6. Live stays dark:
   live_authorized = false
   policy_mode = off
   models/current must not exist
   No training, no promote, no LIVE_TRADING_ACK, no Colab weights.

7. closer_to_autonomous_profit_agent?: NO.
   Ceiling 3→4 only enlarges the denominator. Gate still wants
   20 completed forward trades and 180 forward days.

8. Eleven frozen families remain frozen.
   Multi-symbol funding_carry_fade_v1 stays FROZEN ABSENT.
   Universe is BTCUSDT only. No alts.

9. Pack discipline:
   Prefer the slice-70 deliverable (no restore expected).
   If amputated to slice-61, restore from slice 70 byte-identically
   and record artifacts/slice71_restored_from_slice70.json.
   BTC-only manifest — never broadcast BTC row counts onto ETH/SOL.
   ETH/SOL manifest residue stays residue — do not “fix” provenance.

10. Freshness hygiene:
    human_note_path MUST be docs/human/HUMAN_DATA_NOTE_SLICE71.md
    Finding text MUST say N=9 / nine bars — not leftover “eight”.
    Do not pin live corpus counts to positive integer literals.
    Do not equate live counts to a *dated* artefact with ==.
    Do not cite a test that does not exist.

═══════════════════════════════════════════════════════════════
CROSS-REFERENCE TABLE (prove or fail)
═══════════════════════════════════════════════════════════════
| Claim | Where to prove it |
|---|---|
| OOS untouched | sha256 of slice57_oos_edge_BTCUSDT_summary.json == 28b7dfe0… |
| t1 / folds lock | folds_sha256 == ff5cc8a2… |
| N=9 dates | filter linear time_period_start[:10] > 2026-08-09 |
| no 08-19 linear | that date not in linear |
| prefix intact | first 1461 linear rows still end 2026-08-09 |
| fingerprint | 662de0115880871352d5d623b1020eaa |
| join | last funding at-or-before bar CLOSE |
| 08-17 rate | 0.00009202 unless file shows otherwise |
| 08-18 rate | expect ~0.00003650 (slice-70 funding already had it) |
| gate 2/8 | promotion_gate_allows_live() == False |
| no model | models/current absent |
| 11 freezes | FROZEN_ABSENT count |
| suite | exact pytest counts |
| slice-70 guards | still present or re-run |

Slice 70 close-join rates (re-read files if they differ):
  10: 5.057e-5   11: 8.282e-5   12: 6.601e-5
  13: 7.841e-5   14: 6.29e-6    15: 5.311e-5
  16: 5.00e-5    17: 9.202e-5   18: expect ~3.650e-5

═══════════════════════════════════════════════════════════════
FORBIDDEN (automatic FAIL)
═══════════════════════════════════════════════════════════════
- Synthetic / invented / forward-filled bars labelled real
- Including 2026-08-19 as a closed linear evidence bar
- Relabeling bars with time ≤ t1 as is_forward_observation=true
- Trusting HUMAN_DATA_NOTE without last dates + sha256
- Re-scoring OOS / Stage-1
- Changing constants, schedule, caps, monitors, FUND_ABS, or join
- Raising max_notional_usd above 100
- Arming live, writing models/current, training, Colab promote
- Reopening any of the 11 frozen families / adding alts
- Setting closer_to_autonomous_profit_agent = true
- Treating shadow mean R as Stage-1
- Completing human promotion checklist items in code
- Clamping unfinished exits to corpus end
- Claiming N=9 when after_t1 ≠ 9
- Claiming ceiling = 3 (that was slice 70) when N=9
- Claiming ceiling ≥ 5 when N=9
- Finding text that still says “eight” into a nine-bar window
- Stale human_note_path (SLICE70 left in place)
- Dropping the slice-70 guard family
- “Almost positive”; multi-symbol rescue

═══════════════════════════════════════════════════════════════
STEP 0 — Baseline (clean unzip of tradingbot_slice71_dataready.zip)
═══════════════════════════════════════════════════════════════
0.1 Full pytest baseline — record exact counts.
    Slice 70 final: 4500 passed, 2 skipped.
    One stale live-disk fail is allowed if a prior slice pinned N=8;
    amend under the standing rule (current slice owns live apparatus).
0.2 PYTHONPATH=. python3 tools/print_project_status.py
    Expect: cleared_edge_signal=funding_carry_fade_btc_v1
            live_authorized=false, policy_mode=off
            models_current_present=false, NO EDGE CLAIM intact
0.3 Confirm 11 freezes intact.
0.4 Confirm slice-57 OOS artefact unchanged (do not recompute).
0.5 Hash and date corpora from DISK (authoritative).
0.6 Compare to slice 70: after_t1 8→9, n 1469→1470, last 08-17→08-18.
0.7 promotion_gate_allows_live() False at baseline.
0.8 Pack regression check. Prefer no restore.
0.9 Confirm slice-70 test module still in the tree.

If linear/funding missing → FAIL CLOSED.
If after_t1 still 8 → window DID NOT GROW; do not claim ceiling=4.

═══════════════════════════════════════════════════════════════
STEP 1 — Design note BEFORE any code mutation
═══════════════════════════════════════════════════════════════
EDGE.md next free section:
- Purpose: verify N=9 from FILES; frozen forward shadow; ceiling=4
  declared before scoring; gate stays REFUSE.
- Ceiling: max(0, 9 − 5) = 4 — write this before the tool runs.
- Zero trades at ceiling 4 is admissible.
- Non-goals: no live, no model, no Stage-1 re-score, no FUND_ABS/join move.
- human_note_path = HUMAN_DATA_NOTE_SLICE71.md
- Finding prose: nine bars, not leftover eight.
- closer_to_autonomous_profit_agent: false
- Note: 08-18 close-join in the prior funding file was 0.00003650.
  Likely still no setup. That is information, not a defect.
- Carry slice-70 guards forward.

═══════════════════════════════════════════════════════════════
STEP 2 — Data freshness artefact (files win)
═══════════════════════════════════════════════════════════════
Write artifacts/slice71_data_freshness.json including:
- slice=71, after_t1_linear, after_t1_dates, linear_rows
- delta_since_slice70 (previous 8, grew?, finding says NINE bars)
- linear_sha256, funding_sha256, prefix/append-only
- human_note_path = docs/human/HUMAN_DATA_NOTE_SLICE71.md
- human_note_is_evidence: false
- bars_fabricated: 0
- open_bar_absent_as_expected: true if 2026-08-19 linear not present
- ceiling: maximum_possible_forward_trades_today = 4 for N=9
- ceiling_declared_before_scoring (EDGE.md cite)
- per-decision close-join rates for all 9 dates
- fund_abs_moved / join_changed: false
- pack_base: slice-70 expected

═══════════════════════════════════════════════════════════════
STEP 3 — Forward shadow (frozen pack 58–70)
═══════════════════════════════════════════════════════════════
Produce artifacts/slice71_forward_shadow.json.

Required:
- is_forward_observation = (forward_n_trades > 0) ONLY
- is_stage1_evidence: false
- registration_eligible: false
- cleared_edge_re_scored_this_slice: false
- ceiling max = 4 for N=9; observed ≤ ceiling
- per-bar funding_setup_present (close-join) for all 9 dates
- no exit clamping; last bar cannot fill next_open
- fingerprint 662de0115880871352d5d623b1020eaa
- schedule_mode: one_entry_per_contiguous_run
- caps 1 / 1 / 100.0 / BTCUSDT
- fund_abs_moved_this_slice: false
- join_changed_this_slice: false
- why_neither_moved: cite 08-12, 08-17, AND 08-18
- forward_monitor_status (INSUFFICIENT_DATA OK if 0–4 trades)
  Do NOT copy historical M4 WARN as a new forward reading
- promotion_gate_allows_live: false
- closer_to_autonomous_profit_agent: false
- bars_fabricated: 0

FAIL if schedule drifts, fingerprint differs, exits clamped,
forward_n_trades > ceiling, N=9 and ceiling reported as 3 or 5+.

═══════════════════════════════════════════════════════════════
STEP 4 — Promotion gate (must still REFUSE)
═══════════════════════════════════════════════════════════════
Re-run promotion_gate. Expected allows_live=False, 2/8.
Four possible trades do not complete the gate.
Write artifacts/slice71_promotion_gate.json.
Do not forge human checklist completions.

═══════════════════════════════════════════════════════════════
STEP 5 — Guards + regression
═══════════════════════════════════════════════════════════════
- OOS clear artefact not re-scored
- Forged POSITIVE still refused for all 11 frozen names
- Paper certification still green
- Full pytest; exact counts — write a COMPLETE pytest log (not 925 bytes)
- No models/current; live_authorized false
- Slice-70 guards still enforced
- BTC-only manifest discipline
- human_note_path is SLICE71; finding text says nine / N=9

═══════════════════════════════════════════════════════════════
STEP 6 — Verdict block (mandatory)
═══════════════════════════════════════════════════════════════
SLICE71_VERDICT: PASS | FAIL | PASS_WITH_DEFECTS

extension_present: YES/NO
after_t1_linear_bars: <n>
after_t1_dates: [...]
linear_last: <timestamp>
linear_rows: <n>
new_linear_bars_since_slice70: <n>
the_window_grew: YES/NO
forward_n_trades: <n>
is_forward_observation: true|false
ceiling_max_possible_trades: <n>
within_ceiling: YES/NO
funding_setups_in_window: <n of 9>
08-18_close_join_rate: <from files>
oos_evidence_re_scored: NO
shadow_schedule_one_per_run: YES/NO
constants_fingerprint_match: YES/NO
fund_abs_unchanged: YES
join_unchanged: YES
caps_usd_100: YES/NO
promotion_gate_allows_live: NO
live_authorized: false
models_current_present: false
frozen_absent_count: 11
ProjectStatus.cleared_edge_signal: funding_carry_fade_btc_v1
Closer to autonomous profit agent?: NO

Evidence table: claim → observed → file/command.

PASS if files verify N=9 (or honest shortfall without claiming ceiling=4),
ceiling declared as 4 when N=9, forward count honest (0 or ≥1 both OK),
gate REFUSE, clear intact, pack frozen, suite green, no live/model theatre,
slice-70 guards carried.

FAIL if invented bars, 08-19 as closed linear, false forward, Stage-1
re-score, live/model, FUND_ABS/join/caps/schedule changed, wrong ceiling,
autonomy YES, guards dropped.

Zero trades at N=9 / ceiling=4 is SUCCESSFUL measurement, not a defect.

═══════════════════════════════════════════════════════════════
EXPECTED IF PACK CORRECT
═══════════════════════════════════════════════════════════════
  after_t1_linear = 9
  the_window_grew = true
  ceiling_max_possible_trades = 4
  forward_n_trades = 0 or >=1   (08-18 close-join ~3.65e-5 → likely 0)
  promotion_gate_allows_live = false
  closer_to_autonomous_profit_agent = false

Bank path: still before first completed forward trade.
Gate: 20 trades / 180 days — not this slice.

Execute STEP 0 → 6 in order. Prove with paths, hashes, and artefact JSON.
Be ruthless.
