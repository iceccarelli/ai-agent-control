MISSION — SLICE 75: ONE MORE CLOSED DAY. 08-21 SHOULD FLAG. 08-19 STILL NOT SCOREABLE. MEASURE.

You are the lead trading-systems / risk engineer on a bank-grade desk.
Full compute. Zero licence to invent a business, arm Bybit, add ETH/SOL,
train a model, edit barrier_r_for_all_bars, move FUND_ABS, or claim autonomy.
Execute STEP 0 → 6 in strict order. Fail closed. Bytes beat prose.

You do not invent bars. You do not re-score Stage-1.
You do not silence M4. You do not train models. You do not arm live.
You do not reopen FROZEN_ABSENT. You do not raise notional.
You do not change FUND_ABS, the close-time join, or the frozen barrier function.
You do not expand the universe to ETH/SOL.

Win this slice = TRUTH. Not a fill. Not Bybit. Not dominate.

═══════════════════════════════════════════════════════════════
HOW YOU THINK
═══════════════════════════════════════════════════════════════
Slice 74: after_t1=12, n=1473, last=2026-08-21.
Ladder 2/1/0/0/0/0 — FLAG on 2026-08-19; SETUP on 2026-08-21 (last bar).
No entry: barrier_r_for_all_bars blanks the last 7 bars.
Highest scoreable was 2026-08-14. 08-19 needs last ≥ 2026-08-26.
That function is frozen since slice 35. It scored the n=41 OOS clear.
Do not modify it.

Human supplies closed 2026-08-22 on the slice-74 tree.
N=13 ⇒ ceiling=8. Highest scoreable becomes 2026-08-15.
08-19 still not scoreable. Remaining closed days from 08-22: 23,24,25,26
= FOUR. Slice 74's JSON leftover said "four days short" while last was
08-21 (that was wrong then: five). Compute the gap FROM THE FILE.
Do not copy "four" or "five" from memory.

08-21 MUST become a FLAG if 08-22 is present (no longer last bar).
Same structure 08-19 went through in slice 73. Still not scoreable
until last ≥ 2026-08-28.

08-22 may be a THIRD setup IF 16:00 0.00010000 still stands at close.
Slice 74 already has that print. 08-23 00:00 is AFTER 08-22 close
(23:59:59.999). Reproduce close-join from FILES. Last bar → not a flag.

FUND_ABS=0.0001 is the venue's MODAL / BASE rate (1174/4422 ≈ 26.6%).
§57c corrected the language ("extreme" was wrong). That does NOT
license moving the bar. A threshold changed after seeing which prints
qualify is fitted to data even when dressed as a correction.
Leave FUND_ABS, join, horizon, caps, barrier fn untouched.

is_forward_observation = (forward_n_trades > 0) with a CLOSED trade.
Do not clamp exits. Do not count a second flag as a trade.

STEP 1 must LAND IN THE REPOSITORY. Write EDGE §58, grep it, commit
if git exists, BEFORE deriving any tool.

═══════════════════════════════════════════════════════════════
PURPOSE
═══════════════════════════════════════════════════════════════
| Slice | after_t1 | Ceiling | 08-19 scoreable? | 08-21 | Entries | Closed |
| 73 | 11 | 6 | no | — | 0 | 0 |
| 74 | 12 | 7 | no | SETUP (last bar) | 0 | 0 |
| 75 | THIS expect 13 | 8 | no (need 08-26) | FLAG (not scoreable) | 0 | 0 |

═══════════════════════════════════════════════════════════════
BINDING
═══════════════════════════════════════════════════════════════
1. Signal: funding_carry_fade_btc_v1. Do NOT re-score OOS
   (n=41, M1≈95.13, M2=96.0, mean_r≈+0.1736).
   sha256 artifacts/slice57_oos_edge_BTCUSDT_summary.json =
   28b7dfe0f22c4867661bc73f6434c4acce54a0483c4469a6540155b1b3c18c51
2. t1=2026-08-09T00:00:00Z. Folds ff5cc8a2… unmodified.
3. Expect if pack good:
     after_t1_linear = 13
     dates 2026-08-10 … 2026-08-22
     linear_rows = 1474
     NO 2026-08-23 linear
     funding MUST include 2026-08-22T16:00
   START_MS=1787356800000. 1787443200000 is OPEN — reject as closed linear.
4. Frozen pack: fingerprint 662de0115880871352d5d623b1020eaa
   one_entry_per_contiguous_run; caps $100 / 1 / 1 / BTCUSDT
   FUND_ABS=0.0001; join = last funding at-or-before BAR CLOSE
   barrier_r_for_all_bars UNCHANGED (source line byte-stable vs slice 74).
5. Ceiling BEFORE scores: max(0, N-5). N=13 ⇒ 8. Declare in EDGE before tools.
   Not 7, not 9.
6. live=false; models/current absent; closer_to_autonomous_profit_agent=NO
7. 11 freezes stay closed. No Bybit. No alts. No model.
8. Pack base = slice 74. Restore if amputated.
   BTC-only manifest bump. Do NOT "fix" ETH/SOL residue.
9. Uncompressed funding digest as well as compressed.
10. Carry guards by import. AMEND the fifteen EXPIRES_WITH_DATA names in
    tests/test_slice74_twelve_day_window.py listed in STAGE1_VERDICT_SLICE74.md
    AND in the module itself. Names include:
      test_twelve_closed_bars_lie_after_t1
      test_the_open_bar_is_absent
      test_08_21_is_not_a_flag_because_it_is_the_last_bar
      test_it_needs_the_corpus_to_reach_08_26
    Finding text must say THIRTEEN / N=13, not leftover "twelve".
    human_note_path = docs/human/HUMAN_DATA_NOTE_SLICE75.md
    Do not rename cited tests (slice 74 rule: amend the body, never the name).
11. Complete pytest log with passed/skipped summary.
12. State ladder published. Expect ~3/2/0/0/0/0 IF 08-22 is a setup
    (2 prior setups + 08-22 last-bar setup; flags = 08-19 and 08-21).
    If 08-22 16:00 does NOT stand, setups stay 2 and 08-22 is not a setup.
    Report what FILES say. Do not report a lower rung as a higher one.
13. Gap to 08-19 scoreable: compute from frozen function + last bar.
    Assert remaining closed days == 4 when last is 08-22.
    Do not copy slice 74's "four days short" leftover (that was wrong at N=12).

═══════════════════════════════════════════════════════════════
FORBIDDEN (automatic FAIL)
═══════════════════════════════════════════════════════════════
Invented bars; 08-23 as closed linear; false forward; Stage-1 re-score;
FUND_ABS/join/caps/schedule/horizon/barrier-fn change; live; models/current;
training; Bybit; ETH/SOL; frozen reopen; exit clamping; autonomy YES;
claiming ceiling=7 or 9 at N=13; counting flag/setup/entry as a closed trade;
claiming 08-19 is scoreable; claiming 08-21 is still "not a flag because last bar";
dropping guards; finding text still saying TWELVE; moving FUND_ABS because
it is the modal rate.

═══════════════════════════════════════════════════════════════
STEPS
═══════════════════════════════════════════════════════════════
0 Baseline: pytest (slice 74 final 4775/2); print_project_status;
  11 freezes; OOS sha unchanged; disk after_t1 + sha256; gate False;
  slice74 test module present.
  If after_t1 still 12 → window did not grow; do not claim ceiling=8.
1 Design note IN REPO before code: EDGE §58 exists on disk (grep it).
  Ceiling=8; 08-19 still not scoreable (remaining 4 from last=08-22);
  08-21 should FLAG; 08-22 close-join from FILES; FUND_ABS stays;
  closer=false.
2 artifacts/slice75_data_freshness.json — FILES only; delta vs 74;
  finding says THIRTEEN; note path SLICE75; 08-23 linear absent.
3 artifacts/slice75_forward_shadow.json — frozen pack; ceiling=8;
  per-bar close-join for all 13 dates; ladder; barrier eligibility
  (highest scoreable = 08-15; 08-19 and 08-21 not in the index set);
  entries_taken vs forward_n_trades; no clamp;
  is_forward_observation only if closed trades > 0.
4 promotion_gate → allows_live False.
5 Full pytest; paper cert; no models/current.
6 Verdict:
   SLICE75_VERDICT
   after_t1_linear / dates / the_window_grew
   new_linear_bars_since_slice74
   08-19_flag / 08-19_scoreable (must be NO)
   08-21_flag (must be YES if pack correct) / 08-21_scoreable (NO)
   08-22_close_join_rate / 08-22_setup / 08-22_flag (NO — last bar)
   remaining_closed_days_until_08_19_scoreable (must be 4, from file)
   entries_taken / forward_n_trades / is_forward_observation
   ceiling_max_possible_trades / within_ceiling
   oos_re_scored: NO
   fund_abs_moved: NO
   promotion_gate_allows_live: NO
   closer_to_autonomous_profit_agent: NO

PASS = honest 13 days + 08-19 still not scoreable + 08-21 is a FLAG
       + honest 0 entries + honest 0 closed trades + ceiling 8
       + gate REFUSE + suite green + EDGE §58 on disk before tools.
FAIL = open 08-23 bar, fake forward, re-score, live/model, barrier-fn edit,
       FUND_ABS move, claiming an entry, claiming 08-19 scoreable,
       still calling 08-21 last-bar-not-a-flag, autonomy YES.

Expected if pack correct:
  after_t1_linear = 13
  the_window_grew = true
  ceiling_max_possible_trades = 8
  08-19_scoreable = false
  08-21_flag = true
  08-21_scoreable = false
  entries_taken = 0
  forward_n_trades = 0
  is_forward_observation = false
  remaining_closed_days_until_08_19_scoreable = 4
  promotion_gate_allows_live = false
  closer_to_autonomous_profit_agent = false

Execute STEP 0→6. Bytes beat prose. Be ruthless.
