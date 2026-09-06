MISSION — SLICE 69: ONE MORE CLOSED DAY. MEASURE. DO NOT INVENT A BUSINESS.

You are a senior trading-systems / risk engineer under bank-grade discipline.
Execute STEP 0→6 in order. Full compute. Fail closed. Design before mutation.
Prove every claim with paths, hashes, artefact JSON.

You do not invent bars. You do not re-score Stage-1.
You do not silence M4. You do not train models. You do not arm live.
You do not reopen FROZEN_ABSENT. You do not raise notional.
You do not change FUND_ABS or the close-time join.

PURPOSE
Slice 68: after_t1=6, ceiling=1, 0 of 6 setups, 0 fills.
First informative zero (selectivity only — not skill).
Human missed a calendar day and now supplies closed 2026-08-16.

| Slice | after_t1 | Ceiling | Trades |
| 62–67 | 1→5 | 0 | 0 (non-informative) |
| 68 | 6 | 1 | 0 (quiet; first informative) |
| 69 | THIS — expect 7 | 2 | 0 or ≥1 (both valid) |

BINDING
1. cleared_edge_signal = funding_carry_fade_btc_v1
   OOS frozen: n=41, M1≈95.13, M2=96.0. Do NOT re-score.
   artifacts/slice57_oos_edge_BTCUSDT_summary.json
2. t1 = 2026-08-09T00:00:00Z. Forward = closed bars STRICTLY AFTER t1.
3. Human pack: tradingbot_slice69_dataready.zip
   Note docs/human/HUMAN_DATA_NOTE_SLICE69.md is prose only.
   Verify FILES:
     after_t1_linear = 7
     dates 2026-08-10 … 2026-08-16
     linear last = 2026-08-16
     NO 2026-08-17 closed bar
     n expected 1468
   Pack base should be the slice-68 deliverable.
   If pack is amputated to slice-61, restore + record artefact.
4. Shadow pack FROZEN:
   fingerprint 662de0115880871352d5d623b1020eaa
   one_entry_per_contiguous_run
   caps $100 / 1 pos / 1 entry/day
   M1–M4 UNCHANGED
   FUND_ABS = 0.0001
   Join = last funding at-or-before BAR CLOSE.
   Do NOT rescue the 2026-08-12 00:00 print that equalled FUND_ABS
   and was superseded before close. That is not a defect.
5. Labels:
   extension_present = (after_t1_linear >= 1)
   is_forward_observation = (forward_n_trades > 0)
   Ceiling = max(0, N-5) → N=7 ⇒ 2. Declare BEFORE scoring.
   observed <= ceiling.
6. promotion_gate_allows_live must stay False (20 trades + 180 days + humans).
7. closer_to_autonomous_profit_agent: NO
8. 11 freezes stay closed. No new signal. No alts. No Colab weights.

FORBIDDEN
Open 08-17 bar; invented bars; false forward; Stage-1 re-score;
live; model; FUND_ABS/join change; exit clamping; autonomy YES;
stale human_note_path left as SLICE64.

STEPS
0 Baseline: pytest; print_project_status; 11 freezes; OOS unchanged;
  disk after_t1 + sha256; gate False. Restore if amputated.
1 Design note: ceiling=2 before any score.
2 artifacts/slice69_data_freshness.json — FILES only; delta vs 68;
  bars_fabricated=0; note path = HUMAN_DATA_NOTE_SLICE69.md;
  BTC-only manifest discipline; prefix integrity.
3 artifacts/slice69_forward_shadow.json — frozen pack; ceiling=2;
  per-bar close-join funding_setup_present; no clamp;
  registration_eligible false; is_stage1_evidence false;
  do not rebrand historical M4 WARN as forward.
4 promotion_gate → allows_live False
5 Full pytest; paper cert; no models/current
6 Verdict:
   SLICE69_VERDICT
   after_t1_linear / dates / the_window_grew
   forward_n_trades / is_forward_observation
   ceiling_max_possible_trades / within_ceiling
   funding_setups_in_window
   oos_re_scored: NO
   promotion_gate_allows_live: NO
   closer_to_autonomous_profit_agent: NO

PASS = honest 7 days + honest 0-or-more trades + ceiling 2 + gate locked
FAIL = open bar, fake forward, re-score, live/model, join/FUND_ABS change

Expected if pack correct:
  after_t1_linear = 7
  the_window_grew = true
  ceiling_max_possible_trades = 2
  forward_n_trades = 0 or >=1
  promotion_gate_allows_live = false
  closer_to_autonomous_profit_agent = false

Win this slice = truth. Not a fill. Not autonomy.
Execute STEP 0→6. Bytes beat prose. Be ruthless.
