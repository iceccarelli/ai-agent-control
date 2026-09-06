MISSION — SLICE 74: ONE MORE CLOSED DAY. THE FLAG IS STILL NOT SCOREABLE. MEASURE.

You are the lead trading-systems / risk engineer on a bank-grade desk.
Full compute. Zero licence to invent a business, arm Bybit, add ETH/SOL,
train a model, edit barrier_r_for_all_bars, or claim autonomy.
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
Slice 73: after_t1=11, n=1472, last=2026-08-20.
Ladder 1/1/0/0/0/0 — first FLAG on 2026-08-19.
No entry: barrier_r_for_all_bars blanks the last 7 bars
(eligible[n-horizon-entry_offset-1:]=False). Highest scoreable = 2026-08-13.
Caps refused 0. The flag never reached the schedule.
That function is frozen since slice 35. It scored the n=41 OOS clear.
Do not modify it.

Human supplies closed 2026-08-21 on the slice-73 tree.
08-19 still needs last ≥ 2026-08-26 to become scoreable.
N=12 ⇒ still 4 days short. Expect entries_taken=0.

08-21 may be a SECOND setup IF 16:00 0.00010000 still stands at close.
Reproduce from FILES. 08-22 00:00 is AFTER 08-21 close (23:59:59.999).
Do not treat 08-20 08:00 1e-4 as a decision. Do not assume a fill.

is_forward_observation = (forward_n_trades > 0) with a CLOSED trade.
Do not clamp exits.

STEP 1 must LAND IN THE REPOSITORY. Slice 73 lost §56 to a wrong cwd.
Before deriving any tool: write EDGE §57, grep it, commit if git exists.
A design note that is not on disk is not a design note.

═══════════════════════════════════════════════════════════════
PURPOSE
═══════════════════════════════════════════════════════════════
| Slice | after_t1 | Ceiling | Flag 08-19 scoreable? | Entries | Closed |
| 72 | 10 | 5 | no (last bar) | 0 | 0 |
| 73 | 11 | 6 | no (last−7) | 0 | 0 |
| 74 | THIS expect 12 | 7 | no (need 08-26) | 0 | 0 |

═══════════════════════════════════════════════════════════════
BINDING
═══════════════════════════════════════════════════════════════
1. Signal: funding_carry_fade_btc_v1. Do NOT re-score OOS
   (n=41, M1≈95.13, M2=96.0, mean_r≈+0.1736).
   sha256 artifacts/slice57_oos_edge_BTCUSDT_summary.json =
   28b7dfe0f22c4867661bc73f6434c4acce54a0483c4469a6540155b1b3c18c51
2. t1=2026-08-09T00:00:00Z. Folds ff5cc8a2… unmodified.
3. Expect if pack good:
     after_t1_linear = 12
     dates 2026-08-10 … 2026-08-21
     linear_rows = 1473
     NO 2026-08-22 linear
     funding MUST include 2026-08-21T16:00
   START_MS=1787270400000. 1787356800000 is OPEN — reject as closed linear.
4. Frozen pack: fingerprint 662de0115880871352d5d623b1020eaa
   one_entry_per_contiguous_run; caps $100 / 1 / 1 / BTCUSDT
   FUND_ABS=0.0001; join = last funding at-or-before BAR CLOSE
   barrier_r_for_all_bars UNCHANGED (source line byte-stable vs slice 73).
5. Ceiling BEFORE scores: max(0, N-5). N=12 ⇒ 7. Declare in EDGE before tools.
   Not 6, not 8.
6. live=false; models/current absent; closer_to_autonomous_profit_agent=NO
7. 11 freezes stay closed. No Bybit. No alts. No model.
8. Pack base = slice 73. Restore if amputated.
   BTC-only manifest bump. Do NOT "fix" ETH/SOL residue.
9. Uncompressed funding digest as well as compressed.
10. Carry guards by import. AMEND the twelve EXPIRES_WITH_DATA names in
    tests/test_slice73_eleven_day_window.py listed in STAGE1_VERDICT_SLICE73.md.
    Finding text must say TWELVE / N=12, not leftover “eleven”.
    human_note_path = docs/human/HUMAN_DATA_NOTE_SLICE74.md
11. Complete pytest log with passed/skipped summary.
12. State ladder published. 08-19 flagged, not eligible. Do not report a
    lower rung as a higher one.

═══════════════════════════════════════════════════════════════
FORBIDDEN (automatic FAIL)
═══════════════════════════════════════════════════════════════
Invented bars; 08-22 as closed linear; false forward; Stage-1 re-score;
FUND_ABS/join/caps/schedule/horizon/barrier-fn change; live; models/current;
training; Bybit; ETH/SOL; frozen reopen; exit clamping; autonomy YES;
claiming ceiling=6 or 8 at N=12; counting flag/setup/entry as a closed trade;
claiming 08-19 is scoreable; dropping guards; finding text still saying ELEVEN.

═══════════════════════════════════════════════════════════════
STEPS
═══════════════════════════════════════════════════════════════
0 Baseline: pytest (slice 73 final 4700/2); print_project_status;
  11 freezes; OOS sha unchanged; disk after_t1 + sha256; gate False;
  slice73 test module present.
  If after_t1 still 11 → window did not grow; do not claim ceiling=7.
1 Design note IN REPO before code: EDGE §57 exists on disk (grep it).
  Ceiling=7; 08-19 still not scoreable; 08-21 close-join from FILES;
  closer=false.
2 artifacts/slice74_data_freshness.json — FILES only; delta vs 73;
  finding says TWELVE; note path SLICE74; 08-22 linear absent.
3 artifacts/slice74_forward_shadow.json — frozen pack; ceiling=7;
  per-bar close-join for all 12 dates; ladder; barrier eligibility
  (highest scoreable still before 08-19); entries_taken vs forward_n_trades;
  no clamp; is_forward_observation only if closed trades > 0.
4 promotion_gate → allows_live False.
5 Full pytest; paper cert; no models/current.
6 Verdict:
   SLICE74_VERDICT
   after_t1_linear / dates / the_window_grew
   new_linear_bars_since_slice73
   08-19_flag / 08-19_scoreable (must be NO)
   08-21_close_join_rate / 08-21_setup
   entries_taken / forward_n_trades / is_forward_observation
   ceiling_max_possible_trades / within_ceiling
   oos_re_scored: NO
   promotion_gate_allows_live: NO
   closer_to_autonomous_profit_agent: NO

PASS = honest 12 days + 08-19 still not scoreable + honest 0 entries
       + honest 0 closed trades + ceiling 7 + gate REFUSE + suite green
       + EDGE §57 on disk before tools.
FAIL = open 08-22 bar, fake forward, re-score, live/model, barrier-fn edit,
       claiming an entry, autonomy YES.

Expected if pack correct:
  after_t1_linear = 12
  the_window_grew = true
  ceiling_max_possible_trades = 7
  08-19_scoreable = false
  entries_taken = 0
  forward_n_trades = 0
  is_forward_observation = false
  promotion_gate_allows_live = false
  closer_to_autonomous_profit_agent = false

Execute STEP 0→6. Bytes beat prose. Be ruthless.
