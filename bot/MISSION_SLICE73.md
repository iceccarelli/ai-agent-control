MISSION — SLICE 73: FIRST DAY AN ENTRY IS STRUCTURALLY POSSIBLE. MEASURE. DO NOT INVENT A BUSINESS.

You are the lead trading-systems / risk engineer on a bank-grade desk.
Full compute. Zero licence to invent a business, arm Bybit, add ETH/SOL,
train a model, or claim autonomy.
Execute STEP 0 → 6 in strict order. Fail closed. Bytes beat prose.

You do not invent bars. You do not re-score Stage-1.
You do not silence M4. You do not train models. You do not arm live.
You do not reopen FROZEN_ABSENT. You do not raise notional.
You do not change FUND_ABS or the close-time join.
You do not expand the universe to ETH/SOL (family already ABSENT).

Win this slice = TRUTH. Not a fill. Not Bybit. Not dominate.

═══════════════════════════════════════════════════════════════
HOW YOU THINK
═══════════════════════════════════════════════════════════════
Slice 72 produced the first forward SETUP: 2026-08-19 close-join 0.00010000
== FUND_ABS inclusive, SHORT, stood at 16:00. It could not fill: last bar,
next-open has nowhere to go. flagged=[], candidates=[], entries=0,
forward_n_trades=0, is_forward_observation=false.

Human now supplies closed 2026-08-20. That is the first bar that can be
the 08-19 setup's ENTRY bar. It is not a completed trade. A completed
trade needs an EXIT (08-21 earliest if a barrier hits; 08-25 if horizon).
08-21 is OPEN in this pack and must stay absent as linear.

08-20 is NOT owed as its own setup. Slice 72 already recorded
08-20 08:00 = 0.00010000 (not a decision) and 08-20 16:00 = 0.00009422
(likely close-join, below bar). Reproduce close-join from FILES this slice.
Do not assume a fill. Do not assume a second setup.

is_forward_observation = (forward_n_trades > 0) and a trade has CLOSED.
An entry without an exit is not an observation. Do not clamp exits.

═══════════════════════════════════════════════════════════════
PURPOSE
═══════════════════════════════════════════════════════════════
| Slice | after_t1 | Ceiling | Setups | Entries | Closed trades |
| 62–71 | 1→9 | 0→4 | 0 | 0 | 0 |
| 72 | 10 | 5 | 1 (08-19) | 0 (last bar) | 0 |
| 73 | THIS expect 11 | 6 | ? | 0 or 1 | 0 (no 08-21) |

═══════════════════════════════════════════════════════════════
BINDING
═══════════════════════════════════════════════════════════════
1. Signal: funding_carry_fade_btc_v1. Do NOT re-score OOS
   (n=41, M1≈95.13, M2=96.0, mean_r≈+0.1736, control VALID).
   sha256 artifacts/slice57_oos_edge_BTCUSDT_summary.json =
   28b7dfe0f22c4867661bc73f6434c4acce54a0483c4469a6540155b1b3c18c51
2. t1=2026-08-09T00:00:00Z. Folds ff5cc8a2… unmodified.
   Forward = closed bars STRICTLY AFTER t1.
3. Expect if pack good:
     after_t1_linear = 11
     dates 2026-08-10 … 2026-08-20
     linear_rows = 1472
     NO 2026-08-21 linear
     funding MUST include 2026-08-20T16:00
   START_MS used: 1787184000000. 1787270400000 is OPEN — reject as closed linear.
4. Frozen pack: fingerprint 662de0115880871352d5d623b1020eaa
   one_entry_per_contiguous_run; caps $100 / 1 / 1 / BTCUSDT
   FUND_ABS=0.0001; join = last funding at-or-before BAR CLOSE
   Do not rescue 08-12 00:00, 08-17 9.202e-5, 08-18 3.650e-5.
   Do not treat 08-20 08:00 1e-4 as the decision print.
5. Ceiling BEFORE scores: max(0, N-5). N=11 ⇒ 6.
   Unconditional bound N-1=10 is commentary only; do not replace the declared ceiling.
6. live=false; models/current absent; closer_to_autonomous_profit_agent=NO
7. 11 freezes stay closed. No new signal. No alts. No Bybit.
8. Pack base = slice 72. Restore if amputated; record artefact.
   BTC-only manifest bump. Do NOT "fix" ETH/SOL residue.
9. Compare funding by UNCOMPRESSED digest as well as compressed.
10. Carry guards by import, do not copy.
    AMEND the thirteen EXPIRES_WITH_DATA names in
    tests/test_slice72_ten_day_window.py listed in STAGE1_VERDICT_SLICE72.md.
    Finding text must say ELEVEN / N=11, not leftover “ten”.
    human_note_path = docs/human/HUMAN_DATA_NOTE_SLICE73.md
11. Write a COMPLETE pytest log (must contain the passed/skipped summary).
12. Check slice-72 §55e prediction AGAINST THE FROZEN ARTEFACT, not a fresh rewrite:
    closed 08-20 ⇒ 08-19 setup can acquire an entry bar.
    Entry is not owed. A filled entry is not a result.

═══════════════════════════════════════════════════════════════
FORBIDDEN (automatic FAIL)
═══════════════════════════════════════════════════════════════
Invented bars; 08-21 as closed linear; false forward; Stage-1 re-score;
FUND_ABS/join/caps/schedule/horizon change; live; models/current; training;
Bybit; ETH/SOL expansion; frozen reopen; exit clamping; autonomy YES;
claiming ceiling=5 at N=11 or ceiling=7 at N=11; counting a setup or an
entry as a closed trade; is_forward_observation true when trades=0;
dropping guards; citing missing tests; finding text still saying TEN.

═══════════════════════════════════════════════════════════════
STEPS
═══════════════════════════════════════════════════════════════
0 Baseline: pytest (slice 72 final 4629/2); print_project_status;
  11 freezes; OOS sha unchanged; disk after_t1 + sha256 (compressed AND
  uncompressed funding); gate False; slice72 test module present.
  If after_t1 still 10 → window did not grow; do not claim ceiling=6.
1 Design note BEFORE code: ceiling=6; 08-19 setup from frozen artefact;
  08-20 close-join from FILES; entry possible ≠ entry happened ≠ trade closed;
  closer=false.
2 artifacts/slice73_data_freshness.json — FILES only; delta vs 72;
  new_linear_bars_since_slice72; prefix integrity; bars_fabricated=0;
  finding says ELEVEN bars; note path SLICE73; 08-21 linear absent.
3 artifacts/slice73_forward_shadow.json — frozen pack; ceiling=6;
  per-bar close-join for all 11 dates; 08-19 setup still true;
  report entries_taken separately from forward_n_trades (closed only);
  no clamp; registration_eligible false; is_stage1_evidence false;
  is_forward_observation only if closed trades > 0.
4 promotion_gate → allows_live False (still 2/8). An entry does not complete 20/180.
5 Full pytest; paper cert; no models/current; guards imported and green.
6 Verdict block (mandatory):
   SLICE73_VERDICT
   after_t1_linear / dates / the_window_grew
   new_linear_bars_since_slice72
   forward_n_trades / entries_taken / is_forward_observation
   ceiling_max_possible_trades / within_ceiling
   funding_setups_in_window / 08-19_setup / 08-20_close_join_rate
   oos_re_scored: NO
   promotion_gate_allows_live: NO
   closer_to_autonomous_profit_agent: NO

PASS = honest 11 days + honest 0-or-1 entry + honest 0 closed trades
       (unless a trade actually closed — it must not, no 08-21) + ceiling 6
       + gate REFUSE + clear intact + suite green.
FAIL = open 08-21 bar, fake forward, re-score, live/model, join/FUND_ABS
       change, counting setup/entry as a closed trade, autonomy YES.

Expected if pack correct:
  after_t1_linear = 11
  the_window_grew = true
  ceiling_max_possible_trades = 6
  forward_n_trades = 0
  is_forward_observation = false
  entries_taken = 0 or 1 (both valid; report which and why)
  promotion_gate_allows_live = false
  closer_to_autonomous_profit_agent = false

Execute STEP 0→6. Bytes beat prose. Be ruthless.
